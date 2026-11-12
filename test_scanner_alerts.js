const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { test } = require("node:test");

const html = fs.readFileSync("MLAIStockV2.html", "utf8");
const alertSource = html.slice(html.indexOf("  function freshZoneTime("), html.indexOf("  /* Formatters */"));
const pendingStart = html.indexOf("          msg.alerts.forEach(item => {");
const pendingSource = html.slice(pendingStart, html.indexOf('        } else if (msg.type === "index_update"', pendingStart));

test("live alerts and reconnects share the confirmation window and visible list", () => {
  const confirmed = Date.parse("2026-11-12T13:45:00+05:30");
  let now = confirmed + 15_000;
  let rings = 0;
  let expiry;
  const toasts = new Set();
  const notifications = new Set();
  const acknowledgements = [];
  class Clock extends Date {
    static now() { return now; }
  }
  class DesktopNotification {
    static permission = "granted";
    constructor(title) { this.title = title; notifications.add(this); }
    close() { notifications.delete(this); }
  }
  const context = vm.createContext({
    Date: Clock,
    document: {
      getElementById: () => ({ appendChild: toast => toasts.add(toast) }),
      createElement: () => ({
        style: {}, dataset: {}, addEventListener() {},
        remove() { toasts.delete(this); },
      }),
    },
    Notification: DesktopNotification,
    fetch: (url, options) => {
      assert.equal(url, "/api/alerts/ack");
      acknowledgements.push(JSON.parse(options.body).symbol);
      return Promise.resolve();
    },
    getISTTime: () => new Date("2026-11-12T13:45:15Z"),
    isMarketHours: () => true,
    n: String,
    playBellRangSound: () => { rings++; },
    setTimeout: (callback, delay) => { expiry = { callback, delay }; },
    clearTimeout() {},
    console,
    confirmed,
  });
  const run = source => vm.runInContext(source, context);
  run(`
    const _alertedBreakouts = new Set(), _persistedSeenEventIds = new Set();
    const _getClientId = () => "test";
    const _savePersistedAlertId = id => _persistedSeenEventIds.add(id);
    const signal = (symbol, age) => ({
      symbol, side: "bullish", alert_event_id: symbol,
      is_demand_fresh: true, demand_fresh_time: "01:40 PM", should_alert: true,
      alert_confirmed_epoch: (confirmed - age * 1000) / 1000, entry: 100,
    });
    const live = signal("VISIBLE", 0), hidden = signal("DIXON", 0), old = signal("OLD", 60);
    const legacy = { ...signal("LEGACY", 0), alert_confirmed_epoch: null };
    let _activeTab = "bullish", _displayedScannerRows = [live, old, legacy];
    const _lastScannerPayload = { ok: true, session_date: "2026-11-12", bullish: [live, hidden, old, legacy] };
    ${alertSource}
    function reconnect(msg) { ${pendingSource} }
    checkAndTriggerBellAlerts(_lastScannerPayload);
    reconnect({ alerts: [hidden, old, legacy] });
  `);
  assert.equal(rings, 1);
  assert.deepEqual(acknowledgements, ["VISIBLE"]);
  assert.equal(toasts.size, 1);
  assert.equal(notifications.size, 1);
  assert.equal(expiry.delay, 45_000);
  assert.match([...toasts][0].innerHTML, /Zone 01:40 PM/);
  assert.match([...toasts][0].innerHTML, /Confirmed .* · Alert .* IST/);

  run("_displayedScannerRows = [hidden]; pruneScannerAlerts(); reconnect({ alerts: [hidden] });");
  assert.equal(rings, 2);
  assert.equal(toasts.size, 1);
  assert.equal(notifications.size, 1);
  assert.deepEqual(acknowledgements, ["VISIBLE", "DIXON"]);

  now = confirmed + 60_000;
  expiry.callback();
  assert.equal(toasts.size, 0);
  assert.equal(notifications.size, 0);
  assert.equal(run("isCurrentZoneAlert(hidden)"), false);
  assert.equal(run("isCurrentZoneAlert(hidden, confirmed - 1)"), false);
});

test("Test Bell only invokes the test chime", () => {
  assert.match(html, /getElementById\("testBellBtn"\)\?\.addEventListener\("click", \(\) => playBellRangSound\(true\)\)/);
  assert.doesNotMatch(html, /_testTogglePE|const testSym/);
});
