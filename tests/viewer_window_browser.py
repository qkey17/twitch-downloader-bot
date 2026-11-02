"""Opt-in Chromium/CDP regression and baseline benchmark; synthetic data only.
Run with the existing Python environment (aiohttp) and Chrome; no npm packages.
Results and HTML fixtures go only to --output. No Telegram/Twitch/Oracle calls.
Cache the viewer's unchanged https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/
html2canvas.min.js as <output>/html2canvas.min.js before running PNG checks.
HTTP requests in the browser are intercepted; only synthetic images are served.
"""
import argparse,asyncio,base64,json,re,subprocess,sys,time,uuid
from pathlib import Path
import aiohttp
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from src.html_renderer import render_viewer_html
from viewer_fixtures import synthetic_rows

async def run(args):
    out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    baseline=subprocess.check_output(['git','show',args.baseline+':src/viewer_template.html'],cwd=ROOT).decode('utf-8')
    pixel='data:image/svg+xml;base64,'+base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22"><rect width="22" height="22" fill="purple"/></svg>').decode()
    for count in args.sizes:
        rows=list(synthetic_rows(count))
        for mode in ('online','offline'):
            page=render_viewer_html(rows,'Synthetic title','Synthetic channel','https://www.twitch.tv/videos/123',created_at='2026-10-29T14:28:00Z',mode=mode,local_emotes={'Kappa':pixel},badge_images={'subscriber/1':pixel,'subscriber/2':pixel},captured_during_live=True)
            new_data=re.search(r'<script id="chat-data" type="application/json">(.*?)</script>',page,re.S).group(1)
            old_data=json.dumps(dict(meta=json.loads(new_data)['meta'],rows=rows),ensure_ascii=False).replace('<','\\u003c')
            for version,html in [('new',page),('base',baseline.replace('{{CHAT_DATA_JSON}}',old_data))]:
                # Test-only instrumentation: same decoder/renderer, no production metrics globals.
                html=html.replace('applyFilters();\n</script>','applyFilters().then(()=>{window.viewerMetrics.ready_ms=performance.now()});\n</script>')
                (out/f'{version}-{count}-{mode}.html').write_text(html,encoding='utf-8')
            size_file=out/f'sizes-{count}-{mode}.json'
            size_file.write_text(json.dumps(dict(new_html_bytes=len(page.encode()),base_html_bytes=len(baseline.replace('{{CHAT_DATA_JSON}}',old_data).encode()),new_json_bytes=len(new_data.encode()),base_json_bytes=len(old_data.encode()))),encoding='utf-8')
    profile=out/('profile-'+uuid.uuid4().hex[:8])
    process=subprocess.Popen([args.chrome,'--headless=new','--remote-debugging-port=0','--user-data-dir='+str(profile),'--disable-background-networking','--no-first-run','--enable-precise-memory-info','about:blank'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    results=[]
    try:
        for _ in range(100):
            if (profile/'DevToolsActivePort').exists():break
            await asyncio.sleep(.1)
        port=(profile/'DevToolsActivePort').read_text().splitlines()[0]
        async with aiohttp.ClientSession() as http:
            async with http.get('http://127.0.0.1:'+port+'/json') as response:tabs=await response.json()
            async with http.ws_connect(next(t for t in tabs if t['type']=='page')['webSocketDebuggerUrl'],max_msg_size=64*1024*1024) as ws:
                counter=0;errors=[];requests=[]
                async def call(method,params=None):
                    nonlocal counter
                    counter+=1;wanted=counter
                    await ws.send_json(dict(id=wanted,method=method,params=params or {}))
                    while True:
                        msg=await ws.receive_json(timeout=120)
                        if msg.get('method')=='Fetch.requestPaused':
                            item=msg['params'];url=item['request']['url'];requests.append(url);counter+=1
                            # Use the already downloaded, unchanged html2canvas resource if supplied.
                            canvas=out/'html2canvas.min.js'
                            if 'html2canvas' in url and canvas.exists():
                                method2='Fetch.fulfillRequest';params2=dict(requestId=item['requestId'],responseCode=200,body=base64.b64encode(canvas.read_bytes()).decode(),responseHeaders=[dict(name='Content-Type',value='application/javascript')])
                            elif 'emoticons' in url:
                                method2='Fetch.fulfillRequest';params2=dict(requestId=item['requestId'],responseCode=200,body=pixel.split(',')[1],responseHeaders=[dict(name='Content-Type',value='image/svg+xml'),dict(name='Access-Control-Allow-Origin',value='*')])
                            else:method2='Fetch.failRequest';params2=dict(requestId=item['requestId'],errorReason='BlockedByClient')
                            await ws.send_json(dict(id=counter,method=method2,params=params2))
                        if msg.get('method')=='Runtime.exceptionThrown':errors.append(msg['params']['exceptionDetails'])
                        if msg.get('id')==wanted:
                            assert 'error' not in msg,msg
                            return msg.get('result',{})
                async def evaluate(expr):
                    response=await call('Runtime.evaluate',dict(expression=expr,returnByValue=True,awaitPromise=True))
                    assert 'exceptionDetails' not in response,response
                    return response['result'].get('value')
                async def wait(expr):
                    for _ in range(600):
                        if await evaluate(expr):return
                        await asyncio.sleep(.025)
                    raise AssertionError('Timed out: '+expr)
                version_info=await call('Browser.getVersion');(out/'browser-version.json').write_text(json.dumps(version_info,indent=2),encoding='utf-8')
                await call('Page.enable');await call('Runtime.enable');await call('Fetch.enable',dict(patterns=[dict(urlPattern='http*')]))
                await call('Page.addScriptToEvaluateOnNewDocument',dict(source="""
if(window===top){
window.longTasks=[];
new PerformanceObserver(l=>longTasks.push(...l.getEntries().map(e=>e.duration))).observe({type:'longtask',buffered:true});
const parse=JSON.parse;
JSON.parse=function(text,...args){
  const chat=typeof text==='string' && text.includes('"rows":') && text.includes('"meta":');
  const start=performance.now(),value=parse(text,...args);
  if(chat) window.viewerMetrics={parse_start_ms:start,parse_ms:performance.now()-start,parse_end_ms:performance.now(),sampled_peak_v8:performance.memory?.usedJSHeapSize||null};
  return value;
};
const observer=new MutationObserver(()=>{
  if(!document.querySelector('#list .row'))return;
  observer.disconnect();
  requestAnimationFrame(()=>requestAnimationFrame(()=>{viewerMetrics.first_row_frame_ms=performance.now();}));
});observer.observe(document,{childList:true,subtree:true});
function sample(){
  if(window.viewerMetrics && performance.memory) viewerMetrics.sampled_peak_v8=Math.max(viewerMetrics.sampled_peak_v8||0,performance.memory.usedJSHeapSize);
  if(!window.viewerMetrics?.ready_ms)requestAnimationFrame(sample);
}requestAnimationFrame(sample);
}
"""))
                for width in args.widths:
                    await call('Emulation.setDeviceMetricsOverride',dict(width=width,height=900,deviceScaleFactor=1,mobile=width<600))
                    for count in args.sizes:
                        for mode in ('online','offline'):
                            for version in ('base','new'):
                                errors.clear();start=time.perf_counter()
                                await call('Page.navigate',dict(url=(out/f'{version}-{count}-{mode}.html').as_uri()))
                                await wait("document.readyState==='complete' && document.querySelector('#list .row')!==null")
                                first_row_ms=(time.perf_counter()-start)*1000
                                await wait('!windowBusy && !!window.viewerMetrics?.ready_ms && !!window.viewerMetrics?.first_row_frame_ms')
                                startup=await evaluate('({...viewerMetrics,response_end_ms:performance.getEntriesByType("navigation")[0].responseEnd})')
                                initial=(time.perf_counter()-start)*1000
                                first=await evaluate("document.querySelector('#list .row').getBoundingClientRect().height")
                                max_dom=await evaluate("document.querySelectorAll('#list .row').length")
                                scroll_start=time.perf_counter();jumps=[]
                                await evaluate('longTasks=[];true')
                                if True:
                                    # Walk boundaries in both directions with a visible logical anchor.
                                    for step in range(12):
                                        anchor=await evaluate("(()=>{const n=[...elList.querySelectorAll('.row')].find(n=>+n.dataset.id===Math.min(rowCount()-1,(windowStart+2)*CHUNK+100));if(n){n.scrollIntoView();return +n.dataset.id}return null})()")
                                        await asyncio.sleep(.04);await wait('!windowBusy');await asyncio.sleep(.03)
                                        if anchor is not None:
                                            top=await evaluate(f"[...elList.querySelectorAll('.row')].find(n=>+n.dataset.id==={anchor})?.getBoundingClientRect().top")
                                            assert top is not None and abs(top)<3,(anchor,top)
                                            jumps.append(abs(top))
                                        max_dom=max(max_dom,await evaluate("document.querySelectorAll('#list .row').length"))
                                    for fraction in (.5,.9,1,.7,.3,0):
                                        await evaluate(f'window.scrollTo(0,(document.documentElement.scrollHeight-innerHeight)*{fraction});true')
                                        await asyncio.sleep(.08);await wait('!windowBusy');await asyncio.sleep(.04)
                                        state=await evaluate("({n:elList.querySelectorAll('.row').length,ids:[...elList.querySelectorAll('.row')].map(e=>+e.dataset.id)})")
                                        assert state['n']<=2400 and all(b==a+1 for a,b in zip(state['ids'],state['ids'][1:])),state
                                        max_dom=max(max_dom,state['n'])
                                    await evaluate('window.scrollTo(0,0);true');await asyncio.sleep(.1);await wait('!windowBusy')
                                    assert await evaluate("+elList.querySelector('.row').dataset.id")==0
                                scroll_ms=(time.perf_counter()-scroll_start)*1000
                                scroll_long_task=await evaluate('Math.max(0,...longTasks)')
                                dom=await evaluate("document.querySelectorAll('#list .row').length");max_dom=max(max_dom,dom)
                                await call('HeapProfiler.collectGarbage')
                                heap=(await call('Runtime.getHeapUsage'))['usedSize']
                                list_nodes=await evaluate("elList.querySelectorAll('*').length")
                                search_start=time.perf_counter()
                                await evaluate("elQ.value='unique-last';elQ.dispatchEvent(new Event('input'));true")
                                await wait(f"document.getElementById('stat').textContent==='💬 1 / {count}'")
                                if version=='new':await wait('!windowBusy')
                                assert await evaluate("document.querySelector('#list .row .msg').textContent.includes('unique-last')")
                                search_ms=(time.perf_counter()-search_start)*1000
                                operation_long_task=await evaluate('Math.max(0,...longTasks)')
                                if version=='new':
                                    await evaluate("elQ.value='long';elQ.dispatchEvent(new Event('input'));elQ.value='not-present';elQ.dispatchEvent(new Event('input'));elQ.value='';elU.value='user18';elU.dispatchEvent(new Event('input'));true")
                                    expected=sum((f'user{i if i%7==0 else i%19}').find('user18')>=0 for i in range(count))
                                    await wait(f"elStat.textContent==='💬 {expected} / {count}' && !windowBusy")
                                    assert await evaluate("[...elList.querySelectorAll('.row .user')].every(e=>e.textContent.includes('user18'))")
                                    await evaluate("elU.value='';elU.dispatchEvent(new Event('input'));true");await wait(f"rowCount()==={count} && !windowBusy")
                                    await evaluate('window.scrollTo(0,document.documentElement.scrollHeight*.7);true');await asyncio.sleep(.1);await wait('!windowBusy')
                                    # Mouse drag remains anchored when its starting DOM block is evicted.
                                    await evaluate("getSelection().removeAllRanges();selectionRange=null;projectedSelection=null;true")
                                    xy=await evaluate("({x:elList.getBoundingClientRect().left+120,y:300})")
                                    assert await evaluate('pointAtPointer('+str(xy['x'])+',300)') is not None,'No visible row at pointer target'
                                    await call('Input.dispatchMouseEvent',dict(type='mouseMoved',**xy))
                                    await call('Input.dispatchMouseEvent',dict(type='mousePressed',button='left',buttons=1,clickCount=1,**xy))
                                    for move in range(1,6):
                                        await call('Input.dispatchMouseEvent',dict(type='mouseMoved',x=xy['x']+move*6,y=xy['y']+move*18,buttons=1,button='left'))
                                        await asyncio.sleep(.02)
                                    await asyncio.sleep(.04)
                                    drag_anchor=await evaluate('rememberSelection();selectionRange?.anchor.id');assert drag_anchor is not None,await evaluate("({text:getSelection().toString(),anchor:getSelection().anchorNode?.parentElement?.outerHTML?.slice(0,200),focus:getSelection().focusNode?.parentElement?.outerHTML?.slice(0,200),busy:windowBusy,point:document.elementFromPoint("+str(xy['x'])+","+str(xy['y'])+")?.outerHTML.slice(0,100)})")
                                    await evaluate('window.__dragStart=scrollY;window.scrollBy(0,150000);true');await asyncio.sleep(.15);await wait('!windowBusy')
                                    await call('Input.dispatchMouseEvent',dict(type='mouseMoved',x=xy['x']+30,y=xy['y']+100,buttons=1))
                                    await asyncio.sleep(.04)
                                    await call('Input.dispatchMouseEvent',dict(type='mouseReleased',button='left',buttons=0,clickCount=1,x=xy['x']+30,y=xy['y']+100))
                                    assert await evaluate('selectionRange?.anchor.id')==drag_anchor,'Mouse drag lost its off-window anchor'
                                    assert (await evaluate('selectionRange.focus.id'))>drag_anchor+800,await evaluate('({range:selectionRange,y:scrollY,startY:__dragStart,start:windowStart,end:windowEnd,gesture:selectionGesture,first:elList.firstElementChild.nextElementSibling.getBoundingClientRect().top})')
                                    # A native selection must survive eviction and restore, and PNG must resolve all rows.
                                    await evaluate("(()=>{const n=[...elList.querySelectorAll('.row')];const s=getSelection();s.setBaseAndExtent(n[100].firstChild,0,n[120].lastChild,1);rememberSelection();return true})()")
                                    picked=await evaluate('JSON.parse(JSON.stringify(selectionRange))');assert picked
                                    await evaluate('window.scrollTo(0,0);true');await asyncio.sleep(.1);await wait('!windowBusy')
                                    assert await evaluate('selectionRange.anchor.id')==picked['anchor']['id']
                                    # Extend a native range back into a different DOM window.
                                    await evaluate("getSelection().extend(elList.querySelectorAll('.row')[200].lastChild,1);rememberSelection();true")
                                    assert await evaluate('selectionRange.anchor.id')==picked['anchor']['id']
                                    assert await evaluate('selectionRange.focus.id')==200
                                    await asyncio.sleep(.03);await evaluate('true');requests.clear()
                                    copied=await evaluate("(()=>{const data=new DataTransfer();document.dispatchEvent(new ClipboardEvent('copy',{clipboardData:data,cancelable:true}));window.__richCopied=data.getData('text/html');return data.getData('text/plain').split('\\n').length})()")
                                    await asyncio.sleep(.03);await evaluate('true')
                                    assert not requests,'Inert copy made image requests'
                                    assert copied==picked['anchor']['id']-200+1,copied
                                    assert await evaluate("__richCopied.includes('class=') && !__richCopied.includes('<script>')")
                                    await evaluate('selectionRange='+json.dumps(picked)+';projectSelection();true')
                                    await evaluate('window.scrollTo(0,document.documentElement.scrollHeight*.7);true');await asyncio.sleep(.1);await wait('!windowBusy')
                                    assert await evaluate('selectionRange.focus.id')==picked['focus']['id']
                                    await evaluate("window.__pngRows=null;window.__openSnapshot=openSnapshotOverlay;openSnapshotOverlay=(r)=>{window.__pngRows=Array.from({length:r.length},(_,i)=>r.at(i).text)};elExportBtn.dispatchEvent(new PointerEvent('pointerup',{pointerId:1}));true")
                                    await wait('window.__pngRows!==null')
                                    assert await evaluate('__pngRows.length')==21
                                    assert (await evaluate('__pngRows[0]')).startswith('row-'+str(picked['anchor']['id'])+' ')
                                    await evaluate('openSnapshotOverlay=__openSnapshot;true')
                                    # Exercise the real, existing html2canvas export from original rows.
                                    assert await evaluate("typeof html2canvas==='function'"),'Provide cached html2canvas.min.js in output directory'
                                    png=await evaluate("createSnapshotDataUrl(Array.from({length:3},(_,i)=>rowAt("+str(picked['anchor']['id'])+"+i)),true,true)")
                                    assert png.startswith('data:image/png;base64,')
                                    (out/f'png-{width}-{count}-{mode}.png').write_bytes(base64.b64decode(png.split(',')[1]))
                                    assert await evaluate("document.querySelectorAll('.export-wrap').length")==0
                                    assert await evaluate("[...elList.querySelectorAll('.row')].slice(0,100).every(n=>n.textContent===selectionText(rowAt(+n.dataset.id)))")
                                    if width<600:
                                        before_panel=await evaluate('visibleAnchor()')
                                        await evaluate('syncPanelState(true,false);true');await asyncio.sleep(.15);await wait('!windowBusy')
                                        pos_panel=await evaluate(f"[...elList.querySelectorAll('.row')].find(n=>+n.dataset.id==={before_panel['id']})?.getBoundingClientRect().top")
                                        assert pos_panel is not None and abs(pos_panel-before_panel['top'])<3,(before_panel,pos_panel)
                                        await evaluate('syncPanelState(false,false);true');await asyncio.sleep(.15);await wait('!windowBusy')
                                    # Resize and time toggle must retain the same visible row position.
                                    image_anchor=await evaluate('visibleAnchor()')
                                    await evaluate("(()=>{const image=[...elList.querySelectorAll('.emote')].find(n=>n.getBoundingClientRect().bottom<0);if(image)image.style.height='100px';return true})()")
                                    await asyncio.sleep(.12);await wait('!windowBusy')
                                    image_top=await evaluate(f"[...elList.querySelectorAll('.row')].find(n=>+n.dataset.id==={image_anchor['id']})?.getBoundingClientRect().top")
                                    assert image_top is not None and abs(image_top-image_anchor['top'])<3,(image_anchor,image_top)
                                    before=await evaluate('({...visibleAnchor(),saved:stableAnchor})')
                                    await call('Emulation.setDeviceMetricsOverride',dict(width=390 if width==320 else 320,height=900,deviceScaleFactor=1,mobile=True))
                                    await asyncio.sleep(.2);await wait('!windowBusy')
                                    pos=await evaluate(f"[...elList.querySelectorAll('.row')].find(n=>+n.dataset.id==={before['id']})?.getBoundingClientRect().top")
                                    assert pos is not None and abs(pos-before['top'])<3,(before,pos)
                                    await call('Emulation.setDeviceMetricsOverride',dict(width=width,height=900,deviceScaleFactor=1,mobile=width<600));await asyncio.sleep(.2);await wait('!windowBusy')
                                    await evaluate('elToggleTime.click();true');await wait('!windowBusy')
                                    assert await evaluate("elList.querySelectorAll('.time').length")==0
                                    await evaluate('elToggleTime.click();true');await wait('!windowBusy')
                                    assert await evaluate("elList.querySelectorAll('.row').length<=2400 && !Object.hasOwn(rows[0],'_t')")
                                if version=='new' and count==10000:
                                    await evaluate('window.scrollTo(0,0);true');await asyncio.sleep(.15);await wait('!windowBusy')
                                    shot=await call('Page.captureScreenshot',dict(format='png'));(out/f'page-{width}-{mode}.png').write_bytes(base64.b64decode(shot['data']))
                                assert not errors,errors
                                assert await evaluate('window.fixtureXSS===undefined')
                                result=dict(startup=startup,version=version,width=width,count=count,mode=mode,first_row_ms=round(first_row_ms,1),initial_ms=round(initial,1),scroll_long_task_max_ms=scroll_long_task,search_ms=round(search_ms,1),scroll_scenario_ms=round(scroll_ms,1),max_rows=max_dom,boundary_anchor_error_px=max(jumps,default=0),list_descendants=list_nodes,heap_used_bytes=heap,first_height=first,operation_long_task_max_ms=operation_long_task)
                                results.append(result);(out/'results.json').write_text(json.dumps(results,indent=2),encoding='utf-8');print(json.dumps(result),flush=True)
    finally:
        process.terminate()
        try:process.wait(timeout=8)
        except subprocess.TimeoutExpired:process.kill();process.wait()

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);parser.add_argument('--baseline',default='1a089003b84fdb12c13d419aef16c01d53d8df83');parser.add_argument('--chrome',default=r'C:\Program Files\Google\Chrome\Application\chrome.exe');parser.add_argument('--sizes',nargs='+',type=int,default=[10000,25000,100000]);parser.add_argument('--widths',nargs='+',type=int,default=[320,390,1440])
    asyncio.run(run(parser.parse_args()),loop_factory=asyncio.SelectorEventLoop)
