// Execute the shipped browser script against controlled fetch/timer/DOM boundaries.
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const elements = new Map();
function element() {
  return {value: '', children: [], classList: {add(){}, remove(){}},
    scrollTop: 0, clientHeight: 100, scrollHeight: 100,
    appendChild(x){this.children.push(x)}, removeChild(){this.children.shift()},
    addEventListener(){}, querySelector(){return element()}, setAttribute(){}};
}
const requests = [], timers = new Map(), storage = new Map();
let counter = 0, cleaned = false;
const context = {
  URLSearchParams, console,
  location: {search: '?token=example-token', pathname: '/'},
  history: {replaceState(){cleaned = true}},
  sessionStorage: {getItem:k=>storage.get(k), setItem:(k,v)=>storage.set(k,v)},
  document: {getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id)}, createElement:element},
  setTimeout(fn, ms){const id=++counter; timers.set(id,{fn,ms});return id},
  clearTimeout(id){timers.delete(id)}, confirm:()=>true, prompt:()=>null,
  fetch(url, options){return new Promise((resolve, reject)=>requests.push({url,options,resolve,reject}))},
};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), context);
const flush = () => new Promise(resolve=>setImmediate(resolve));
function reply(request, body, status=200){request.resolve({status,json:()=>Promise.resolve(body)})}
function pollingTimers(){return [...timers].filter(([,v])=>v.fn.name==='poll')}
function tickPoll(){const [id,t]=pollingTimers()[0];timers.delete(id);t.fn()}
(async()=>{
  assert(cleaned);
  assert.equal(requests.length,1);
  assert.equal(requests[0].options.headers['X-Fish-Token'],'example-token');
  // Repeated controls during an in-flight poll must not start another poll.
  elements.get('b-wake').onclick();elements.get('b-wake').onclick();
  reply(requests[1],{ok:true});reply(requests[2],{ok:true});await flush();
  assert.equal(requests.filter(x=>x.url.startsWith('/api/snapshot')).length,1);
  reply(requests[0],{ok:true,events:[],jobs:[],next_cursor:400,has_more:true});await flush();
  assert.equal(pollingTimers().length,1);tickPoll();
  assert.equal(requests[3].url,'/api/snapshot?after=400');
  reply(requests[3],{ok:true,events:[],jobs:[],next_cursor:800,has_more:false});await flush();
  assert.equal(pollingTimers().length,1);
  const input=elements.get('msg');input.value='keep on failure';
  elements.get('b-say').onclick();elements.get('b-say').onclick();
  assert.equal(requests.filter(x=>x.url==='/api/say').length,1);
  requests[4].reject(new Error('offline'));await flush();assert.equal(input.value,'keep on failure');
  elements.get('b-say').onclick();reply(requests[5],{ok:true});await flush();
  assert.equal(input.value,'');assert.equal(pollingTimers().length,1);
  console.log('Web token, single poll chain, pagination cursor and input retention passed');
})().catch(error=>{console.error(error);process.exitCode=1});
