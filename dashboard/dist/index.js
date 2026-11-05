/**
 * AO Bridge — Hermes Dashboard Plugin (frontend)
 *
 * Kanban board over the local AO daemon: every AO project's sessions as cards,
 * spawn form, send-message sheet, kill action. Polls the plugin backend at
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
    return h("span", { className: "ao-chip ao-chip-harness" }, props.harness);
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

  function Card(props) {
    const card = props.card;
    const [busy, setBusy] = useState(null);
    const [err, setErr] = useState(null);

    function action(kind) {
      setBusy(kind); setErr(null);
      const url = kind === "kill"
        ? API + "/sessions/" + encodeURIComponent(card.id) + "/kill"
        : null;
      authedFetchJson(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })
        .then(function () { props.onChanged(); })
        .catch(function (e) { setErr(parseApiErrorMessage(e)); })
        .finally(function () { setBusy(null); });
    }

    return h("div", { className: "ao-card ao-card-" + (COLUMN_CLASS[card.column] || "") },
      h("div", { className: "ao-card-top" },
        h("span", { className: "ao-card-project", title: card.projectId || "standalone" }, card.project),
        h("span", { className: "ao-card-age", title: card.createdAt }, timeAgo(card.createdAt))
      ),
      h("div", { className: "ao-card-name", title: card.displayName }, card.displayName),
      h("div", { className: "ao-card-meta" },
        h(HarnessChip, { harness: card.harness }),
        h(StatusChip, { displayStatus: card.displayStatus, status: card.status }),
        card.branch ? h("span", { className: "ao-chip ao-chip-branch", title: card.branch }, card.branch) : null
      ),
      h(PrLinks, { prs: card.prs }),
      err ? h("div", { className: "ao-card-error" }, err) : null,
      h("div", { className: "ao-card-actions" },
        card.column !== "archive" && card.column !== "done"
          ? h("button", {
              className: "ao-btn ao-btn-ghost", disabled: !!busy,
              onClick: function () { props.onSend(card); },
            }, "Message")
          : null,
        card.column !== "archive"
          ? h("button", {
              className: "ao-btn ao-btn-danger", disabled: !!busy,
              title: "Kill this AO session",
              onClick: function () {
                if (window.confirm("Kill AO session \"" + card.displayName + "\"? This terminates the agent session.")) action("kill");
              },
            }, busy === "kill" ? "Killing…" : "Kill")
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
                  return h(Card, { key: c.id, card: c, onChanged: props.onChanged, onSend: props.onSend });
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
          h("select", { className: "ao-input", value: harness,
                        onChange: function (e) { setHarness(e.target.value); } },
            (props.harnesses || ["hermes"]).map(function (x) {
              return h("option", { key: x, value: x }, x);
            })
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

  // --- Board page ----------------------------------------------------------

  function AoBridgePage() {
    const [data, setData] = useState(null);
    const [error, setError] = useState(null);
    const [loaded, setLoaded] = useState(false);
    const [refreshedAt, setRefreshedAt] = useState(null);
    const [spawnOpen, setSpawnOpen] = useState(false);
    const [sendTarget, setSendTarget] = useState(null);
    const [collapsed, setCollapsed] = useState(function () {
      try { return window.localStorage.getItem("ao-bridge.collapsed") === "archive"; }
      catch (_e) { return false; }
    });
    const timerRef = useRef(null);

    const refresh = useCallback(function () {
      return authedFetchJson(API + "/board")
        .then(function (body) {
          setData(body); setError(null);
          setRefreshedAt(new Date());
        })
        .catch(function (e) { setError(parseApiErrorMessage(e)); })
        .finally(function () { setLoaded(true); });
    }, []);

    useEffect(function () {
      refresh();
      timerRef.current = window.setInterval(refresh, POLL_MS);
      return function () { if (timerRef.current) window.clearInterval(timerRef.current); };
    }, [refresh]);

    function toggleArchive() {
      setCollapsed(function (prev) {
        const next = !prev;
        try { window.localStorage.setItem("ao-bridge.collapsed", next ? "archive" : ""); } catch (_e) {}
        return next;
      });
    }

    const columns = useMemo(function () {
      if (!data) return [];
      const buckets = {};
      (data.cards || []).forEach(function (c) {
        (buckets[c.column] = buckets[c.column] || []).push(c);
      });
      return (data.columns || []).map(function (col) {
        const bucket = buckets[col.id] || [];
        bucket.sort(function (a, b) { return (b.createdAt || "").localeCompare(a.createdAt || ""); });
        return { id: col.id, label: col.label, cards: bucket };
      });
    }, [data]);

    return h("div", { className: "ao-bridge" },
      h("div", { className: "ao-header" },
        h("div", { className: "ao-header-title" },
          h("span", { className: "ao-header-name" }, "AO Bridge"),
          data && data.baseUrl
            ? h("span", { className: "ao-header-daemon", title: data.baseUrl }, "daemon " + data.baseUrl.replace(/^https?:\/\//, ""))
            : null,
          refreshedAt ? h("span", { className: "ao-header-updated" }, "updated " + refreshedAt.toLocaleTimeString()) : null
        ),
        h("div", { className: "ao-header-actions" },
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
                collapsed: col.id === "archive" && collapsed,
                onToggle: col.id === "archive" ? toggleArchive : undefined,
                onChanged: refresh, onSend: setSendTarget,
              });
            })
          ),
      spawnOpen
        ? h(SpawnForm, {
            projects: (data && data.projects) || [], harnesses: (data && data.harnesses) || ["hermes"],
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
        : null
    );
  }

  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === "function") {
    window.__HERMES_PLUGINS__.register("ao-bridge", AoBridgePage);
  }
})();
