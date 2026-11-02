const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const html=fs.readFileSync(path.join(__dirname,'../src/viewer_template.html'),'utf8');
const script=html.split('<script>')[1].split('</script>')[0];
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
  const ctx=vm.createContext({});vm.runInContext(part('function matches(', 'async function applyFilters('),ctx);
  assert.ok(ctx.matches({user:'USER',text:'Hello Мир'},'мир','user'));
  assert.equal(ctx.matches({user:'OTHER',text:'Hello'},'hello','user'),false);
  assert.ok(ctx.matches({},'',''));assert.equal(ctx.matches({},'x',''),false);
});
test('new query cancels old full-chat scan before committing results',async()=>{
  let commits=0;
  const ctx=vm.createContext({Uint32Array,performance,rows:Array.from({length:100000},(_,i)=>({user:'user',text:'row '+i})),FILTER_TOKEN:0,selectionGesture:{active:true},elQ:{value:'row'},elU:{value:''},elStat:{},window:{getSelection:()=>({removeAllRanges(){}}),scrollTo(){}},yieldViewer:()=>new Promise(r=>setTimeout(r,0)),showWindow:async()=>{commits++},ensureEmotesLoaded(){}});
  vm.runInContext('let filtered=null;const rowCount=()=>filtered===null?rows.length:filtered.length;'+part('function matches(', "document.addEventListener('scroll'"),ctx);
  const first=ctx.applyFilters();ctx.elQ.value='row 99999';const second=ctx.applyFilters();
  await Promise.all([first,second]);assert.equal(ctx.selectionGesture,null);assert.equal(commits,1);assert.equal(ctx.elStat.textContent,'💬 1 / 100000');
});
test('clearing query reuses payload without a second full-length array',async()=>{
  const rows=[{text:'hello'}];const ctx=vm.createContext({rows,elQ:{value:''},elU:{value:''},elStat:{},window:{getSelection:()=>({removeAllRanges(){}}),scrollTo(){}},showWindow:async()=>{},ensureEmotesLoaded(){}});
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
