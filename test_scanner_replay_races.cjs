const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');

class Element {
  constructor(){this.children=[];this.listeners={};this.value='';this.textContent='';this.hidden=false;this.disabled=false;}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=[...nodes];}
  addEventListener(name,fn){this.listeners[name]=fn;}
}
function harness(){
  const ids=[...fs.readFileSync('scanner_replay.html','utf8').matchAll(/id="([^"]+)"/g)].map(x=>x[1]);
  const nodes=Object.fromEntries(ids.map(id=>[id,new Element()]));
  nodes['row-filter'].value='retained';
  const pending=[];
  const context={Intl,Date,console,encodeURIComponent,document:{getElementById:id=>nodes[id],createElement:()=>new Element(),createDocumentFragment:()=>new Element()},fetch:(path,opts)=>new Promise(resolve=>pending.push({path,opts,respond:(data,status=200)=>resolve({ok:status>=200&&status<300,status,json:async()=>data}),invalid:(status=503)=>resolve({ok:false,status,json:async()=>{throw Error('Invalid JSON');}})})),WebSocket:()=>{throw Error('No live WS allowed');},localStorage:{setItem:()=>{throw Error('No browser storage');}}};
  vm.runInNewContext(fs.readFileSync('scanner_replay.js','utf8'),context);
  return {nodes,pending};
}
const settle=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};
const sessions=(date,source='scanner_event_log')=>({ok:true,source,sessions:date?[{date,snapshot_count:1}]:[]});
const snapshots=(day,id)=>({ok:true,source:'scanner_event_log',session_date:day,snapshots:[{id,at:day+'T09:25:00+05:30',integrity:'complete'}]});
const snapshot=(day,id)=>({ok:true,source:'scanner_event_log',snapshot_id:id,observed_at_ist:day+'T09:25:00+05:30',integrity:'complete',coverage:{recorded_rows:0,expected_rows:0},bullish:[],bearish:[],issues:[]});
async function loaded(h){h.pending[0].respond(sessions('2026-10-21'));await settle();h.pending[1].respond(snapshots('2026-10-21','scan_start'));await settle();h.pending[2].respond(snapshot('2026-10-21','scan_start'));await settle();}
function cleared(nodes){assert.equal(nodes['snapshot-id'].textContent,'—');assert.equal(nodes['selected-heading'].textContent,'Archive unavailable');for(const id of ['session-select','snapshot-select','snapshot-range','previous','next','stock-search','row-filter'])assert.equal(nodes[id].disabled,true,id);}

test('first visitor automatically loads archive using anonymous GETs only',async()=>{
  const h=harness();await loaded(h);const {nodes,pending}=h;
  assert.equal(nodes['snapshot-id'].textContent,'scan_start');assert.equal(nodes['error'].hidden,true);
  assert.equal(pending.length,3);
  for(const r of pending){assert.equal(r.opts.method,'GET');assert.equal(r.opts.credentials,'omit');assert.equal(r.opts.headers,undefined);assert.equal(r.opts.body,undefined);assert.ok(r.path.startsWith('/api/scanner_replay/'));}
  for(const id of ['unlock-form','access-token','forget-browser','session-access'])assert.equal(nodes[id],undefined);
});

test('empty archive is not misrepresented as missing authentication',async()=>{
  const {nodes,pending}=harness();pending[0].respond(sessions(null));await settle();
  assert.equal(pending.length,1);assert.equal(nodes['selected-heading'].textContent,'No recorded sessions available');assert.equal(nodes['error'].hidden,true);assert.equal(nodes['session-select'].disabled,true);
});

test('unavailable archive has no live/local fallback or stale navigation controls',async()=>{
  const {nodes,pending}=harness();pending[0].respond({ok:false,error:'replay_database_unavailable'},503);await settle();
  cleared(nodes);assert.equal(pending.length,1);assert.match(nodes['error'].textContent,/No live snapshot/);
});

test('current day failure clears previously displayed archive and navigation',async()=>{
  const h=harness();await loaded(h);h.nodes['session-select'].listeners.change({target:{value:'2026-10-20'}});
  h.pending[3].respond({ok:false,error:'replay_unavailable'},503);await settle();cleared(h.nodes);
});

test('invalid JSON snapshot failure clears rows and controls without login fallback',async()=>{
  const {nodes,pending}=harness();pending[0].respond(sessions('2026-10-21'));await settle();pending[1].respond(snapshots('2026-10-21','scan_start'));await settle();pending[2].invalid();await settle();
  cleared(nodes);assert.match(nodes['error'].textContent,/could not be read/);assert.equal(pending.length,3);
});

test('local recorded preview still identifies itself and loads anonymously',async()=>{
  const {nodes,pending}=harness();pending[0].respond(sessions(null,'local_recorded_scanner_preview'));await settle();
  assert.equal(pending.length,1);assert.equal(pending[0].opts.credentials,'omit');assert.match(nodes['source-label'].textContent,/LOCAL RECORDED SCANNER PREVIEW/);assert.match(nodes['source-label'].textContent,/not production history/);
});

test('older day response cannot repopulate rows under a newer day',async()=>{
  const h=harness();await loaded(h);const {nodes,pending}=h;
  nodes['session-select'].listeners.change({target:{value:'2026-10-19'}});nodes['session-select'].listeners.change({target:{value:'2026-10-20'}});
  pending[4].respond(snapshots('2026-10-20','scan_latest'));await settle();pending[5].respond(snapshot('2026-10-20','scan_latest'));await settle();pending[3].respond(snapshots('2026-10-19','scan_older'));await settle();
  assert.equal(nodes['selected-heading'].textContent,'2026-10-20 · 09:25:00 IST');assert.equal(nodes['snapshot-id'].textContent,'scan_latest');assert.equal(pending.length,6);
});

test('older day failure cannot erase a newer successful scan',async()=>{
  const h=harness();await loaded(h);const {nodes,pending}=h;
  nodes['session-select'].listeners.change({target:{value:'2026-10-19'}});nodes['session-select'].listeners.change({target:{value:'2026-10-20'}});
  pending[4].respond(snapshots('2026-10-20','scan_latest'));await settle();pending[5].respond(snapshot('2026-10-20','scan_latest'));await settle();pending[3].respond({ok:false,error:'replay_unavailable'},503);await settle();
  assert.equal(nodes['snapshot-id'].textContent,'scan_latest');assert.equal(nodes['error'].hidden,true);assert.equal(nodes['session-select'].disabled,false);
});

test('older snapshot response cannot overwrite newer selected scan',async()=>{
  const {nodes,pending}=harness();pending[0].respond(sessions('2026-10-21'));await settle();const list=snapshots('2026-10-21','scan_first');list.snapshots.push({id:'scan_latest',at:'2026-10-21T09:30:00+05:30',integrity:'complete'});pending[1].respond(list);await settle();
  nodes['snapshot-select'].listeners.change({target:{value:'1'}});pending[3].respond(snapshot('2026-10-21','scan_latest'));await settle();pending[2].respond(snapshot('2026-10-21','scan_first'));await settle();
  assert.equal(nodes['snapshot-id'].textContent,'scan_latest');assert.equal(nodes['error'].hidden,true);
});
