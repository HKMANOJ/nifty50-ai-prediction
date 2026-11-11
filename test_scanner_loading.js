/* Offline tests of the actual dashboard loader and snapshot status functions. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('MLAIStockV2.html', 'utf8');
const statusSource = html.slice(html.indexOf('  let _lastGoodRenderAt'), html.indexOf('  function renderAll('));
const loaderSource = html.slice(html.indexOf('  function loadData('), html.indexOf('  /* ── Tab Switchers'));
const nodes = {
  scannerDataStatus: {style: {}}, navbarRefreshStatus: {style: {}}, sessionDataBanner: {style: {}},
  sensexVal: {style: {}, closest: () => null}, refreshBtn: {},
};
const timers = new Map(); let timerId = 0; let calls = 0; let options; let response;
let marketOpen = false;
const context = vm.createContext({
  window: {}, performance: {now: () => 1},
  console: {error() {}}, Date, AbortController,
  document: {visibilityState: 'visible', getElementById: id => nodes[id]},
  setTimeout: (fn, delay) => { const id = ++timerId; timers.set(id, {fn, delay}); return id; },
  clearTimeout: id => timers.delete(id),
  fetch: async (url, opts) => { calls++; options = {url, ...opts}; return await response(); },
  isMarketHours: () => marketOpen,
  n: number => String(number), pct: number => `${number}%`,
});
vm.runInContext(`const API_BASE = ''; const refreshBtn = document.getElementById('refreshBtn');
  ${statusSource}
  function renderAll(p) {
    if (!p?.ok || !Array.isArray(p.bullish) || !Array.isArray(p.bearish)) return false;
    _lastScannerPayload = p; scannerSnapshotStatus(p); return true;
  }
  ${loaderSource}`, context);
const run = source => vm.runInContext(source, context);
const payload = {ok:true, bullish:[{symbol:'TEST'}], bearish:[], generated_at_ist:'2026-10-07T15:30:00+05:30'};
async function main() {
  response = async () => { throw new Error('offline'); };
  await run('loadData()');
  assert.match(nodes.scannerDataStatus.textContent, /unavailable.*retrying/i);
  assert.equal([...timers.values()][0].delay, 5000); // retries also after close
  response = async () => ({ok:false, status:503});
  await run('loadData()');
  assert.equal([...timers.values()][0].delay, 10000);
  response = async () => ({ok:true, json:async () => ({ok:false})});
  await run('loadData()');
  assert.match(nodes.scannerDataStatus.textContent, /No verified snapshot/);
  response = async () => ({ok:true, json:async () => payload});
  await run('loadData()');
  assert.equal(timers.size, 0);
  assert.match(nodes.scannerDataStatus.textContent, /Market-closed snapshot — NOT live/);
  assert.match(nodes.scannerDataStatus.textContent, /1 bullish \/ 0 bearish/);
  assert.equal(options.method, 'GET');
  response = async () => { throw new Error('offline again'); };
  await run('loadData()');
  assert.match(nodes.scannerDataStatus.textContent, /previous data retained/);
  assert.equal(run('_lastScannerPayload.bullish[0].symbol'), 'TEST');
  assert.equal([...timers.values()][0].delay, 5000);
  let resolve;
  response = () => new Promise(r => { resolve = r; });
  const count = calls;
  const first = run('loadData()'); const second = run('loadData()');
  assert.equal(calls, count + 1); // mobile lifecycle bursts coalesce
  resolve({ok:true, json:async () => payload});
  await Promise.all([first, second]);
  response = async () => ({ok:true, json:async () => payload});
  await run('loadData({refresh:true})');
  assert.equal(options.method, 'GET');
  assert.match(nodes.navbarRefreshStatus.textContent, /Session snapshot loaded/);
  marketOpen = true;
  response = () => new Promise(r => { resolve = r; });
  const background = run('loadData()');
  assert.equal(nodes.refreshBtn.disabled, false);
  const manual = run('loadData({refresh:true})');
  assert.equal(run('loadData({refresh:true})'), manual);
  assert.equal(nodes.refreshBtn.disabled, true);
  assert.equal(options.method, 'GET');
  response = async () => ({ok:true, json:async () => payload});
  resolve({ok:true, json:async () => payload});
  await Promise.all([background, manual]);
  assert.equal(options.method, 'POST');
  assert.equal(nodes.refreshBtn.disabled, false);
  assert.match(nodes.navbarRefreshStatus.textContent, /Scanner refreshed/);
  response = async () => ({ok:false, status:409});
  await run('loadData({refresh:true})');
  assert.match(nodes.navbarRefreshStatus.textContent, /scan is already running/i);
  response = async () => ({ok:true, json:async () => payload});
  await run('loadData()');
  assert.match(nodes.navbarRefreshStatus.textContent, /connection restored/i);
  run('scannerSnapshotStatus({session_date:"2026-11-11", _previous_session:true, bullish:[], bearish:[]})');
  assert.match(nodes.sessionDataBanner.textContent, /2026-11-11.*awaiting the current session/);
  run('renderIndexQuotes({SENSEX:{ltp:81503.85, percent_change:0.49, as_of_epoch:200}})');
  assert.match(nodes.sensexVal.textContent, /81503\.85.*0\.49%/);
  run('renderIndexQuotes({SENSEX:{ltp:0}}); renderIndexQuotes({SENSEX:{ltp:80000, as_of_epoch:100}})');
  assert.match(nodes.sensexVal.textContent, /81503\.85/);
  run('renderIndexQuotes({SENSEX:{ltp:81600, as_of_epoch:300}}, {fallback:true})');
  assert.equal(nodes.sensexVal.textContent, '81600');
  for (const match of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
  for (const event of ['online', 'pageshow', 'visibilitychange']) assert.ok(html.includes(`addEventListener("${event}"`));
  assert.ok(html.includes('Date.parse(payload.generated_at_ist) < Date.parse(_lastScannerPayload.generated_at_ist)'));
  console.log('Scanner loading tests passed (offline, HTTP error, invalid payload, retries, retention, coalescing, snapshot status, lifecycle hooks, syntax).');
}
main().catch(e => { console.error(e); process.exitCode = 1; });
