"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const storage = new Map();
const timers = [];
const requests = [];
let callback, observerCalls = 0, failNext = false;
const rowElement = {
  isConnected: true, getClientRects: () => [{}], setAttribute() {},
};
const document = {
  visibilityState: "visible", addEventListener() {}, querySelectorAll: () => [rowElement],
};
const sandbox = {
  window: {}, document, crypto: {randomUUID: () => "browser-session-test-123456"},
  sessionStorage: {getItem: k => storage.get(k), setItem: (k, v) => storage.set(k, v)},
  IntersectionObserver: class {
    constructor(cb) { callback = cb; }
    disconnect() {}
    observe() { observerCalls++; }
  },
  requestAnimationFrame: fn => fn(), setTimeout: fn => { timers.push(fn); return timers.length; },
  fetch: async (url, options) => {
    requests.push({url, body: JSON.parse(options.body)});
    if (failNext) { failNext = false; throw new Error("offline"); }
    return {ok: true, status: 200};
  },
};
vm.runInNewContext(fs.readFileSync("scanner_display_telemetry.js", "utf8"), sandbox);
const telemetry = sandbox.window.ScannerDisplayTelemetry;
const payload = {session_date: "2026-10-07", snapshot_id: "scan_" + "a".repeat(32)};
const rows = [{signal_id: "sig_" + "b".repeat(24), side: "bullish"}];
const container = {querySelectorAll: () => [rowElement]};
const entry = {target: rowElement, isIntersecting: true, intersectionRatio: 0.5};
const settle = async () => { await new Promise(resolve => setImmediate(resolve)); };

(async () => {
  telemetry.configure(payload, "");
  telemetry.bind(container, "tr", rows, "ranking", "bullish");
  assert.equal(observerCalls, 1);
  document.visibilityState = "hidden";
  callback([entry]);
  assert.equal(timers.length, 0, "background tabs must not generate visibility evidence");
  document.visibilityState = "visible";
  callback([{...entry, isIntersecting: false}]);
  assert.equal(timers.length, 0, "offscreen rows must not generate evidence");
  rowElement.isConnected = false;
  callback([entry]);
  assert.equal(timers.length, 0, "detached rows must not generate evidence");
  rowElement.isConnected = true;
  callback([entry]);
  callback([entry]);
  assert.equal(timers.length, 1, "repeated observations must not create duplicate requests");
  failNext = true;
  timers.shift()();
  await settle();
  assert.equal(requests.length, 1);
  assert.equal(timers.length, 1, "offline evidence must be retained for retry");
  timers.shift()();
  await settle();
  assert.equal(requests.length, 2);
  assert.equal(requests[1].body.receipts.length, 1);
  const receipt = requests[1].body.receipts[0];
  assert.equal(receipt.signal_id, rows[0].signal_id);
  assert.equal(receipt.display_rank, 1);
  assert.equal(receipt.panel, "ranking");
  assert.equal(receipt.page_visible, true);
  callback([entry]);
  assert.equal(timers.length, 0, "acknowledged rows must stay deduplicated");
  telemetry.bind(container, "div", rows, "instant_triggers");
  callback([entry]);
  timers.shift()();
  await settle();
  assert.equal(requests.length, 3, "a different panel gets its own first visibility receipt");
  telemetry.configure({...payload, session_date: "2026-10-08"}, "");
  telemetry.bind(container, "tr", rows, "ranking", "bullish");
  callback([entry]);
  timers.shift()();
  await settle();
  assert.equal(requests.length, 4, "session rollover must reset visibility deduplication");
  telemetry.configure({...payload, session_date:"2026-10-08", snapshot_id:"scan_"+"c".repeat(32)}, "");
  const secondContainer = {querySelectorAll: () => [{...rowElement}, rowElement]};
  telemetry.bind(secondContainer,"tr",[rows[0],rows[0]],"ranking","bullish");
  callback([entry]);
  timers.shift()();
  await settle();
  assert.equal(requests[4].body.receipts[0].display_rank,2,"new snapshot captures rank movement");
  telemetry.trace("load_failed",{transport:"http",reason:"network_error"});
  timers.shift()();
  await settle();
  assert.ok(requests[5].url.endsWith("client_events"));
  assert.equal(requests[5].body.events[0].kind,"load_failed");
  console.log("PASS: visibility, detached rows, retries, deduplication, panel and session boundaries");
})().catch(error => { console.error(error); process.exitCode = 1; });
