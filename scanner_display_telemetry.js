/* Visibility evidence only: no changes to scoring, ranking or alerts. */
(function () {
  "use strict";
  let payload = null;
  let apiBase = "";
  let timer = null;
  let sending = false;
  const queued = new Map();
  let seen = new Set();
  let storageKey = "";
  let clientId;
  try {
    clientId = sessionStorage.getItem("scanner_client_session");
    if (!clientId) {
      clientId = crypto.randomUUID();
      sessionStorage.setItem("scanner_client_session", clientId);
    }
  } catch (_) {
    clientId = "browser_" + Date.now() + "_" + Math.random().toString(36).slice(2);
  }
  const context = new WeakMap();
  const observer = typeof IntersectionObserver === "function" ? new IntersectionObserver(entries => {
    if (document.visibilityState !== "visible") return;
    for (const entry of entries) {
      if (!entry.isIntersecting || entry.intersectionRatio < 0.1) continue;
      const receipt = context.get(entry.target);
      if (!receipt || !entry.target.isConnected || entry.target.getClientRects().length === 0) continue;
      const identity = receipt.session_date + "|" + receipt.panel + "|" + receipt.signal_id;
      if (seen.has(identity) || queued.has(identity)) continue;
      if (queued.size >= 1000) continue;
      queued.set(identity, {...receipt, rendered_at: new Date().toISOString(),
                            page_visible: true, in_viewport: true});
      schedule(250);
    }
  }, {threshold: 0.1}) : null;

  function schedule(delay) {
    if (!timer) timer = setTimeout(() => { timer = null; flush(); }, delay);
  }

  async function flush() {
    if (sending || !queued.size) return;
    sending = true;
    const batch = [...queued.entries()].slice(0, 100);
    try {
      const response = await fetch(`${apiBase}/api/stock_suggestions/display_receipts`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({receipts: batch.map(([, receipt]) => receipt)}), keepalive: true,
      });
      if (response.ok) {
        for (const [identity] of batch) { seen.add(identity); queued.delete(identity); }
        try { sessionStorage.setItem(storageKey, JSON.stringify([...seen].slice(-2000))); } catch (_) {}
      } else if (response.status === 400 || response.status === 404) {
        // A legacy server or expired snapshot cannot acknowledge this batch.
        for (const [identity] of batch) queued.delete(identity);
      }
    } catch (_) {
      // Retain evidence for a bounded retry; never interrupt the dashboard.
    } finally {
      sending = false;
      if (queued.size) schedule(5000);
    }
  }

  function observePaintedRows() {
    if (!observer) return;
    requestAnimationFrame(() => requestAnimationFrame(() => {
      observer.disconnect();
      document.querySelectorAll("[data-scanner-receipt]").forEach(el => observer.observe(el));
    }));
  }

  window.ScannerDisplayTelemetry = {
    configure(nextPayload, base) {
      payload = nextPayload;
      apiBase = base;
      const nextKey = "scanner_display_seen_" + payload.session_date;
      if (nextKey !== storageKey) {
        storageKey = nextKey;
        queued.clear();
        try { seen = new Set(JSON.parse(sessionStorage.getItem(storageKey) || "[]")); }
        catch (_) { seen = new Set(); }
      }
    },
    bind(container, selector, rows, panel, side) {
      if (!observer || !payload?.snapshot_id || !payload?.session_date) return;
      container.querySelectorAll(selector).forEach((el, index) => {
        const row = rows[index];
        if (!row || !/^sig_[a-f0-9]{24}$/.test(row.signal_id || "")) return;
        context.set(el, {signal_id: row.signal_id, snapshot_id: payload.snapshot_id,
                         session_date: payload.session_date, panel,
                         side: side || row.side, display_rank: panel === "ranking" ? index + 1 : null,
                         client_session_id: clientId});
        el.setAttribute("data-scanner-receipt", "");
      });
      observePaintedRows();
    },
  };
  document.addEventListener("visibilitychange", observePaintedRows);
})();
