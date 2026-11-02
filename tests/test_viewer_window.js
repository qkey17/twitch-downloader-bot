const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const html=fs.readFileSync(path.join(__dirname,'../src/viewer_template.html'),'utf8');
const script=html.split('<script>')[1].split('</script>')[0];
const legacyAccess="const COMPACT_DATA=false;"+script.slice(script.indexOf('function messageUser('),script.indexOf('const MODE ='));
function part(start,end){return script.slice(script.indexOf(start),script.indexOf(end));}
test('embedded JS is valid and payload is not eagerly decorated',()=>{
  new vm.Script(script);
  assert.ok(!script.includes('r._t =') && !script.includes('r._u ='));
});
test('measured and estimated block heights include short final block',()=>{
  const ctx=vm.createContext({heights:[500,750],averageHeight:2,blockLength:b=>b===2?3:800});
  vm.runInContext(part('function heightBefore(', 'function updateSpaces('),ctx);
  assert.equal(ctx.heightBefore(0),0);assert.equal(ctx.heightBefore(1),500);
  assert.equal(ctx.heightBefore(3),1256);
  ctx.heights[1]=0;assert.equal(ctx.heightBefore(3),506);
});
test('search uses original full rows and case-insensitive text/user matching',()=>{
  const ctx=vm.createContext({});vm.runInContext(legacyAccess+part('function matches(', 'async function applyFilters('),ctx);
  assert.ok(ctx.matches({user:'USER',text:'Hello Мир'},'мир','user'));
  assert.equal(ctx.matches({user:'OTHER',text:'Hello'},'hello','user'),false);
  assert.ok(ctx.matches({},'',''));assert.equal(ctx.matches({},'x',''),false);
});
test('new query cancels old full-chat scan before committing results',async()=>{
  let commits=0;
  const ctx=vm.createContext({Uint32Array,performance,rows:Array.from({length:100000},(_,i)=>({user:'user',text:'row '+i})),FILTER_TOKEN:0,selectionGesture:{active:true},elQ:{value:'row'},elU:{value:''},elStat:{},window:{getSelection:()=>({removeAllRanges(){}}),scrollTo(){}},yieldViewer:()=>new Promise(r=>setTimeout(r,0)),showWindow:async()=>{commits++},resetScrubber(){},ensureEmotesLoaded(){}});
  vm.runInContext(legacyAccess+'let filtered=null;const rowCount=()=>filtered===null?rows.length:filtered.length;'+part('function matches(', "document.addEventListener('scroll'"),ctx);
  const first=ctx.applyFilters();ctx.elQ.value='row 99999';const second=ctx.applyFilters();
  await Promise.all([first,second]);assert.equal(ctx.selectionGesture,null);assert.equal(commits,1);assert.equal(ctx.elStat.textContent,'💬 1 / 100000');
});
test('clearing query reuses payload without a second full-length array',async()=>{
  const rows=[{text:'hello'}];const ctx=vm.createContext({rows,elQ:{value:''},elU:{value:''},elStat:{},window:{getSelection:()=>({removeAllRanges(){}}),scrollTo(){}},showWindow:async()=>{},resetScrubber(){},ensureEmotesLoaded(){}});
  vm.runInContext('let filtered=null,FILTER_TOKEN=0;const rowCount=()=>filtered===null?rows.length:filtered.length;'+part('async function applyFilters(', "document.addEventListener('scroll'"),ctx);
  await ctx.applyFilters();assert.equal(vm.runInContext('filtered',ctx),null);
  assert.equal(ctx.elStat.textContent,'💬 1 / 1');
});

test('off-window copy projects the same text as rows without images',()=>{
  const ctx=vm.createContext({SHOW_TIME:true,LOCAL_EMOTES:{Kappa:'local.png'},CDN_EMOTES:{},EMOTES:new Map()});
  vm.runInContext(part('function catalogEmoteUrl(', 'function renderTextWithEmotes(')+part('function selectionText(', "document.addEventListener('copy'"),ctx);
  const r={t:'01 : 02 : 03',user:'User',text:'a Kappa b <script> & 🎮'};
  assert.equal(ctx.selectionText(r),'01:02:03User:a  b <script> & 🎮');
  r.fragments=[{text:'Kappa',emoteID:'25'},{text:' safe & text'}];
  assert.equal(ctx.selectionText(r),'01:02:03User: safe & text');
  ctx.SHOW_TIME=false;assert.equal(ctx.selectionText(r),'User: safe & text');
});

test('PNG selection keeps only range/indices, decoding one requested row at a time',()=>{
  let decoded=0;
  const ctx=vm.createContext({rows:['a','b','c'],filtered:[2,0],decodeMessage:r=>{decoded++;return r}});
  vm.runInContext(part('function snapshotRows(', 'async function buildSnapshotWrap('),ctx);
  const selected=ctx.snapshotRows(0,1);assert.equal(decoded,0);assert.equal(selected.length,2);
  ctx.filtered=null;assert.equal(selected.at(0),'c');assert.equal(selected.at(1),'a');assert.equal(decoded,2);
});

function sliderContext(count=10000){
  const events={},frames=[],attributes={},times=[{style:{}},{style:{}}],percents=[{},{}];let mounted=0,activeBuilders=0,maxBuilders=0;
  const ctx=vm.createContext({count,filtered:null,meta:{duration_seconds:21088},FILTER_TOKEN:0,FILTERING:false,windowBusy:false,CHUNK:800,averageHeight:40,stableAnchor:null,scrollIntent:0,resizeAnchor:null,
    MOBILE_MQ:{matches:true,addEventListener(){}},clampNumber:(n,a,b)=>Math.min(b,Math.max(a,n)),
    rowCount:()=>ctx.count,rowAt:i=>({t:ctx.time ?? '01 : 42 : 15'}),
    requestAnimationFrame:fn=>{frames.push(fn);return frames.length},yieldViewer:async()=>{},
    elScrubber:{clientWidth:300,getBoundingClientRect:()=>({width:300}),classList:{add(){},remove(){}},style:{setProperty(k,v){attributes[k]=v}},setAttribute(k,v){attributes[k]=v},
      querySelectorAll:s=>s==='.scrub-times'?times:percents,
      querySelector:()=>({style:{},getBoundingClientRect:()=>({left:0,width:180})}),
      addEventListener:(name,fn)=>events[name]=fn,hasPointerCapture:()=>false,releasePointerCapture(){},setPointerCapture(){}},
    elList:{getBoundingClientRect:()=>({top:0}),querySelector:s=>{mounted=+s.match(/\d+/)[0];return {getBoundingClientRect:()=>({top:75,bottom:100})}}},
    getComputedStyle:()=>({paddingTop:'75'}),heightBefore:()=>0,viewportInset:()=>75,innerHeight:900,
    document:{documentElement:{scrollHeight:100000}},window:{scrollY:0,scrollTo(){},scrollBy(){}},
    visibleAnchor:()=>({id:mounted,top:75,inset:75}),scheduleWindow(){},
    showWindow:async b=>{activeBuilders++;maxBuilders=Math.max(maxBuilders,activeBuilders);await new Promise(r=>setTimeout(r,1));activeBuilders--}
  });
  vm.runInContext(part('/* Logical-index mobile navigation.', '/* Selection endpoints belong')+`;globalThis.slider={scrubTarget,scrubPercent,scrubTime,durationTime,setScrubIndex,requestScrub,resetScrubber,sync:scheduleScrubSync,get state(){return {index:scrubIndex,active:scrubActive(),running:scrubRunning,pending:scrubPending,gesture:scrubGesture}}}`,ctx);
  return {ctx,slider:ctx.slider,events,attributes,times,percents,get maxBuilders(){return maxBuilders},
    async drain(){for(let i=0;i<100;i++){frames.splice(0).forEach(fn=>fn());await new Promise(r=>setTimeout(r,2));if(!frames.length && !ctx.slider.state.active)return;}throw Error('Slider did not settle');}};
}
test('logical slider clamps endpoints, rounds indexes and handles empty/single sets',()=>{
  const {slider:s}=sliderContext();
  for(const n of [0,1,10000,25000,100000]){
    assert.equal(s.scrubTarget(-1,n),0);assert.equal(s.scrubTarget(2,n),Math.max(0,n-1));
    assert.equal(s.scrubPercent(0,n),0);if(n>1)assert.equal(s.scrubPercent(n-1,n),100);
    assert.equal(s.scrubTarget(.63,n),n>1?Math.round(.63*(n-1)):0);
  }
  assert.equal(s.scrubTime('01 : 42 : 15'),'01:42:15');
  for(const invalid of [undefined,null,12,'','2026-01-01','01:99:00','<img src=x>'])assert.equal(s.scrubTime(invalid),'—');
});
test('slider text and ARIA describe the filtered logical row, neutral when empty',()=>{
  const h=sliderContext(3);h.slider.setScrubIndex(1);
  assert.equal(h.attributes['aria-valuenow'],'50');assert.equal(h.times[0].textContent,'01:42:15 / 05:51:28');
  assert.equal(h.attributes['aria-valuetext'],'01:42:15 / 05:51:28');
  h.ctx.count=0;h.slider.setScrubIndex(0);assert.equal(h.attributes['aria-disabled'],'true');assert.equal(h.times[0].textContent,'— / 05:51:28');
  h.ctx.count=1;h.ctx.time='invalid';h.slider.setScrubIndex(99);assert.equal(h.slider.state.index,0);assert.equal(h.times[1].textContent,'— / 05:51:28');
});
test('rapid requests coalesce to latest index with one serial builder',async()=>{
  const h=sliderContext(100000);for(let i=0;i<500;i++)h.slider.requestScrub(i*173);
  await h.drain();assert.equal(h.slider.state.index,499*173);assert.equal(h.maxBuilders,1);
});
test('in-flight transition is superseded and filter reset cancels old indices',async()=>{
  const h=sliderContext();h.slider.requestScrub(9000);
  // Begin build, then request a different final row before the builder resolves.
  const first=h.drain();h.slider.requestScrub(100);await first;
  assert.equal(h.slider.state.index,100);assert.equal(h.maxBuilders,1);
  h.slider.requestScrub(8000);h.ctx.FILTER_TOKEN++;h.ctx.FILTERING=true;h.slider.resetScrubber();h.ctx.count=1;
  await h.drain();assert.equal(h.slider.state.index,0);assert.equal(h.slider.state.active,false);
});
test('pointer cancellation/lost capture and keyboard have bounded final targets',async()=>{
  const h=sliderContext(10000),e={pointerId:1,button:0,isPrimary:true,clientX:120,preventDefault(){},stopPropagation(){}};
  h.events.pointerdown(e);assert.ok(h.slider.state.gesture);
  h.events.pointermove({...e,clientX:260});const chosen=h.slider.state.index;
  h.events.pointercancel({...e,type:'pointercancel'});await h.drain();assert.equal(h.slider.state.index,chosen);assert.equal(h.slider.state.gesture,null);
  h.events.keydown({...e,key:'End'});await h.drain();assert.equal(h.slider.state.index,9999);
  h.events.keydown({...e,key:'Home'});await h.drain();assert.equal(h.slider.state.index,0);
  h.events.keydown({...e,key:'ArrowRight'});await h.drain();assert.equal(h.slider.state.index,1);
});

test('a release over the slider without its own gesture reaches existing selection cleanup',()=>{
  const h=sliderContext();let stopped=false;
  h.events.pointerup({pointerId:5,type:'pointerup',stopPropagation(){stopped=true}});
  assert.equal(stopped,false);
});

test('chosen visible row near the clamped bottom remains stable until actual scroll',async()=>{
  const h=sliderContext(20);h.ctx.window.scrollY=1;h.ctx.document.documentElement.scrollHeight=900;
  h.slider.requestScrub(18);await h.drain();assert.equal(h.slider.state.index,18);
  h.ctx.window.scrollY=2;h.slider.sync();await h.drain();assert.equal(h.slider.state.index,19);
});

test('duration and timestamps normalize unlimited hours without inventing unknown values',()=>{
  const {slider:s}=sliderContext();assert.equal(s.durationTime(435088),'120:51:28');
  assert.equal(s.scrubTime('105 : 42 : 15'),'105:42:15');assert.equal(s.scrubTime('5:02:03'),'05:02:03');
  for(const bad of [null,undefined,true,'5h',-1,NaN,Infinity,3.4,0])assert.equal(s.durationTime(bad),'—');
});
test('physical end uses full duration only without filters, restores time after scrolling up',()=>{
  const h=sliderContext(20);h.ctx.document.documentElement.scrollHeight=1000;h.ctx.window.scrollY=100;
  h.slider.setScrubIndex(18);assert.equal(h.times[0].textContent,'05:51:28 / 05:51:28');
  h.ctx.window.scrollY=95;h.slider.setScrubIndex(18);assert.equal(h.times[0].textContent,'01:42:15 / 05:51:28');
  h.ctx.filtered=[1,2];h.ctx.window.scrollY=100;h.slider.setScrubIndex(1);assert.equal(h.times[0].textContent,'01:42:15 / 05:51:28');
  h.ctx.meta.duration_seconds=null;h.slider.setScrubIndex(1);assert.equal(h.times[0].textContent,'01:42:15 / —');
});
test('relative gesture starts anywhere without jumping and clamps movement from the original position',async()=>{
  const h=sliderContext(10001),e={pointerId:1,button:0,isPrimary:true,clientX:50,preventDefault(){},stopPropagation(){}};
  h.slider.setScrubIndex(8000);h.events.pointerdown(e);assert.equal(h.slider.state.index,8000);
  h.events.pointerup({...e,type:'pointerup'});assert.equal(h.slider.state.pending,null);assert.equal(h.slider.state.index,8000);
  h.events.pointerdown(e);h.events.pointermove({...e,clientX:77.8});assert.equal(h.slider.state.index,9000);
  h.events.pointermove({...e,clientX:150});assert.equal(h.slider.state.index,10000);
  h.events.pointermove({...e,clientX:22.2});assert.equal(h.slider.state.index,7000);
  h.events.lostpointercapture({...e,type:'lostpointercapture'});await h.drain();assert.equal(h.slider.state.index,7000);
});
