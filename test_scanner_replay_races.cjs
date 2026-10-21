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
  const context={Intl,Date,console,encodeURIComponent,document:{getElementById:id=>nodes[id],createElement:()=>new Element(),createDocumentFragment:()=>new Element()},fetch:(path,opts)=>new Promise(resolve=>pending.push({path,opts,respond:(data,status=200)=>resolve({ok:status>=200&&status<300,status,json:async()=>data})})),WebSocket:()=>{throw Error('No live WS allowed');},localStorage:{setItem:()=>{throw Error('No token storage');}}};
  vm.runInNewContext(fs.readFileSync('scanner_replay.js','utf8'),context);
  return {nodes,pending};
}
const settle=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};
const sessions=date=>({ok:true,source:'local_recorded_scanner_preview',sessions:[{date,snapshot_count:1}]});
const snapshots=(day,id)=>({ok:true,source:'local_recorded_scanner_preview',session_date:day,snapshots:[{id,at:day+'T09:25:00+05:30',integrity:'complete'}]});
const snapshot=(day,id)=>({ok:true,source:'local_recorded_scanner_preview',snapshot_id:id,observed_at_ist:day+'T09:25:00+05:30',integrity:'complete',coverage:{recorded_rows:0,expected_rows:0},bullish:[],bearish:[],issues:[]});

test('older unlock failure cannot overwrite a newer successful sessions load',async()=>{
  const {nodes,pending}=harness();
  pending[0].respond({ok:false,error:'replay_auth_required'},401);await settle();
  assert.equal(nodes['unlock-form'].hidden,false);
  nodes['access-token'].value='first-test-key';nodes['unlock-form'].listeners.submit({preventDefault(){}});
  nodes['access-token'].value='new-test-key';nodes['unlock-form'].listeners.submit({preventDefault(){}});
  assert.equal(nodes['access-token'].value,'');
  pending[2].respond(sessions('2026-10-21'));await settle();
  pending[3].respond(snapshots('2026-10-21','scan_new'));await settle();
  pending[4].respond(snapshot('2026-10-21','scan_new'));await settle();
  const heading=nodes['selected-heading'].textContent;
  pending[1].respond({ok:false,error:'replay_auth_required'},401);await settle();
  assert.equal(nodes['selected-heading'].textContent,heading);
  assert.equal(nodes['error'].hidden,true);
  assert.equal(nodes['unlock-form'].hidden,true);
  assert.ok(pending.every(r=>r.opts.method==='GET'&&r.path.startsWith('/api/scanner_replay/')&&!r.path.includes('test-key')));
});

test('older day response cannot repopulate rows under a newer day',async()=>{
  const {nodes,pending}=harness();
  pending[0].respond(sessions('2026-10-21'));await settle();
  pending[1].respond(snapshots('2026-10-21','scan_start'));await settle();
  pending[2].respond(snapshot('2026-10-21','scan_start'));await settle();
  nodes['session-select'].listeners.change({target:{value:'2026-10-19'}});
  nodes['session-select'].listeners.change({target:{value:'2026-10-20'}});
  assert.match(nodes['selected-heading'].textContent,/Loading/);
  pending[4].respond(snapshots('2026-10-20','scan_latest'));await settle();
  pending[5].respond(snapshot('2026-10-20','scan_latest'));await settle();
  pending[3].respond(snapshots('2026-10-19','scan_older'));await settle();
  assert.equal(nodes['selected-heading'].textContent,'2026-10-20 · 09:25:00 IST');
  assert.equal(nodes['snapshot-id'].textContent,'scan_latest');
});
