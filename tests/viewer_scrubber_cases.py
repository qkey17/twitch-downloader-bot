"""Fast-scrubber cases using viewer_window_browser's existing CDP transport/fixtures."""
import asyncio,base64,json,time

async def check_scrubber(call,evaluate,wait,out,args,errors,requests):
    results=[]
    async def settled():
        await wait('!FILTERING && !windowBusy && !scrubActive() && !scrubFrame && !scrubSyncFrame')
        await asyncio.sleep(.18)
        await wait('!windowBusy && !scrubActive()')
    async def metrics(width):
        await call('Emulation.setDeviceMetricsOverride',dict(width=width,height=900,deviceScaleFactor=1,mobile=width<600))
        await call('Emulation.setTouchEmulationEnabled',dict(enabled=width<600,maxTouchPoints=1))
    async def load(width,count,mode='offline',version='new'):
        await metrics(width);errors.clear();requests.clear()
        await call('Page.navigate',dict(url=(out/f'{version}-{count}-{mode}.html').as_uri()))
        await wait("document.readyState==='complete' && typeof windowBusy!=='undefined' && !windowBusy")
        if version=='new':await settled()
    async def track():
        return await evaluate("(()=>{const r=elScrubber.getBoundingClientRect();const t=elScrubber.querySelector('.scrub-track').getBoundingClientRect();return {left:t.left+7,width:t.width-14,y:r.top+r.height/2}})()")
    async def mouse(fractions,release=True):
        r=await track()
        def pt(f):return dict(x=r['left']+r['width']*f,y=r['y'])
        await call('Input.dispatchMouseEvent',dict(type='mousePressed',button='left',buttons=1,clickCount=1,**pt(fractions[0])))
        for f in fractions[1:]:
            await call('Input.dispatchMouseEvent',dict(type='mouseMoved',button='left',buttons=1,**pt(f)))
            await asyncio.sleep(.006)
        if release:await call('Input.dispatchMouseEvent',dict(type='mouseReleased',button='left',buttons=0,clickCount=1,**pt(fractions[-1])))
    async def touch(fractions,cancel=False):
        r=await track()
        for i,f in enumerate(fractions):
            await call('Input.dispatchTouchEvent',dict(type='touchStart' if i==0 else 'touchMove',touchPoints=[dict(x=r['left']+r['width']*f,y=r['y'],id=1,radiusX=8,radiusY=8)]))
            await asyncio.sleep(.012)
        await call('Input.dispatchTouchEvent',dict(type='touchCancel' if cancel else 'touchEnd',touchPoints=[]))
    async def verify_target(index):
        await settled()
        state=await evaluate('''(()=>{const id='''+str(index)+''',n=elList.querySelector(`.row[data-id="${id}"]`),r=n?.getBoundingClientRect();return {id:scrubIndex,top:r?.top??null,bottom:r?.bottom??null,inset:viewportInset(),rows:elList.querySelectorAll('.row').length,visible:visibleAnchor(),max:window.__maxRows||0,active:scrubActive(),busy:windowBusy,pct:elScrubber.getAttribute('aria-valuenow'),time:elScrubber.querySelector('.scrub-time').textContent,expected:scrubTime(rowAt(id).t)}})()''')
        assert state['top'] is not None and state['bottom']>state['inset'] and state['top']<900,(index,state)
        assert state['id']==index,(index,state)
        assert state['time']==state['expected'],state
        assert state['rows']<=2400 and (state['max'] or 0)<=2400,state
        return state
    async def key(key):
        await evaluate('elScrubber.focus();true')
        await call('Input.dispatchKeyEvent',dict(type='keyDown',key=key,code=key))
        await call('Input.dispatchKeyEvent',dict(type='keyUp',key=key,code=key))
    async def shot(name):
        await evaluate('document.activeElement?.blur();true')
        data=await call('Page.captureScreenshot',dict(format='png'))
        (out/(name+'.png')).write_bytes(base64.b64decode(data['data']))
    async def panel():
        r=await evaluate("(()=>{const r=elPanelHandle.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2}})()")
        for event in ('mousePressed','mouseReleased'):
            await call('Input.dispatchMouseEvent',dict(type=event,button='left',buttons=1 if event=='mousePressed' else 0,clickCount=1,**r))
        await settled()
    # No change to desktop geometry, including existing controls/messages.
    def geometry_js():return "JSON.stringify(['header','.panel-top','.panel-stats','.controls','.panel-actions','#list .row'].map(s=>{const r=document.querySelector(s).getBoundingClientRect();return [s,r.x,r.y,r.width,r.height]}))"
    await load(1440,10000,version='base');base=await evaluate(geometry_js())
    await load(1440,10000);assert await evaluate(geometry_js())==base,(base,await evaluate(geometry_js()))
    assert await evaluate("getComputedStyle(elScrubber).display==='none' && getComputedStyle(elPanelHandle).display==='none'")
    results.append(dict(case='desktop-geometry',status='PASS'))
    # Lightweight instrumentation only in fixtures; observe actual DOM and builder concurrency.
    instrument="""window.__maxRows=0;window.__builders=0;window.__maxBuilders=0;
new MutationObserver(()=>{__maxRows=Math.max(__maxRows,elList.querySelectorAll('.row').length)}).observe(elList,{childList:true,subtree:true});
window.__show=showWindow;showWindow=async(...args)=>{__builders++;__maxBuilders=Math.max(__maxBuilders,__builders);try{return await __show(...args)}finally{__builders--}};true"""
    for width in args.widths:
        if width>=900:continue
        for count in args.sizes:
            for mode in ('offline','online'):
                await load(width,count,mode);await evaluate(instrument)
                assert await evaluate("elHeader.offsetHeight<=80 && [...elHeader.children].filter(n=>getComputedStyle(n).display!=='none').every(n=>n===elScrubber || n===elPanelHandle)")
                assert await evaluate("elScrubber.offsetHeight>=44 && document.documentElement.scrollWidth===innerWidth")
                start=time.perf_counter()
                for f in (1,0,.1,.9,.37,.75,.25,.5):
                    await mouse([f]);await verify_target(round(f*(count-1)))
                await mouse([.2,.9999]);near_end=round(.9999*(count-1));await verify_target(near_end)
                await panel();await verify_target(near_end);await panel();await verify_target(near_end)
                await mouse([.5]);await settled()
                # Real continuous drag with reversals; current held value cannot be overwritten by scroll.
                await mouse([.5,.8,.2,.95,.05,.8,.63],release=False)
                held=await evaluate('scrubIndex');await evaluate('window.scrollBy(0,100);true');await asyncio.sleep(.05)
                assert await evaluate('scrubIndex')==held
                r=await track();await call('Input.dispatchMouseEvent',dict(type='mouseReleased',button='left',buttons=0,clickCount=1,x=r['left']+.63*r['width'],y=r['y']))
                await verify_target(held)
                await touch([.2,.8,.1,.9]);await verify_target(round(.9*(count-1)))
                await touch([.4,.6],cancel=True);await verify_target(round(.6*(count-1)))
                # Lost capture, then independent next gesture, cannot leave active drag behind.
                await mouse([.3,.7],release=False)
                await evaluate('elScrubber.releasePointerCapture(scrubGesture.id);true')
                r=await track();await call('Input.dispatchMouseEvent',dict(type='mouseMoved',buttons=1,x=r['left']+.7*r['width'],y=r['y']))
                await settled();assert await evaluate('scrubGesture===null')
                r=await track();await call('Input.dispatchMouseEvent',dict(type='mouseReleased',button='left',buttons=0,x=r['left'],y=r['y']))
                await key('Home');await verify_target(0)
                await key('End');await verify_target(count-1)
                await key('Home');await key('ArrowRight');await verify_target(1)
                await key('PageDown');await verify_target(1+round((count-1)/10))
                # Ordinary vertical document scroll follows the first visible logical row.
                await mouse([.45]);await settled();await evaluate('window.scrollBy(0,700);true');await settled()
                assert await evaluate('scrubIndex===visibleAnchor().id')
                # Panel reflow preserves same logical row and relative position below header.
                before=await evaluate('visibleAnchor()');index=await evaluate('scrubIndex')
                await panel();assert await evaluate('PANEL_EXPANDED')
                after=await evaluate('visibleAnchor()')
                assert before['id']==after['id'] and abs((before['top']-before['inset'])-(after['top']-after['inset']))<3,(before,after)
                assert await evaluate('scrubIndex')==index
                await mouse([.8]);await verify_target(round(.8*(count-1)))
                if count==10000 and mode=='offline':
                    for f in (0,.25,.5,.75,1):
                        await mouse([f]);await verify_target(round(f*(count-1)))
                        assert await evaluate("(()=>{const r=elScrubber.querySelector('.scrub-track').getBoundingClientRect(),d=elScrubber.querySelector('.scrub-dot').getBoundingClientRect();return Math.abs(d.left+d.width/2-r.left-r.width/2)<.1})()")
                        await shot(f'expanded-{width}-{int(f*100)}')
                    await mouse([.8]);await settled();await shot(f'expanded-{width}')
                before=await evaluate('visibleAnchor()')
                await panel();assert not await evaluate('PANEL_EXPANDED')
                after=await evaluate('visibleAnchor()');assert before['id']==after['id'],(before,after)
                # Delayed image size change and width reflow above visible row.
                await evaluate("(()=>{const image=[...elList.querySelectorAll('.emote')].find(n=>n.getBoundingClientRect().bottom<viewportInset());if(image)image.style.height='100px';return true})()")
                await settled();assert await evaluate('visibleAnchor().id')==before['id'],(before,await evaluate('({anchor:visibleAnchor(),stable:stableAnchor,resize:resizeAnchor})'))
                await metrics(390 if width==320 else 320);await settled()
                assert await evaluate('visibleAnchor().id')==before['id'],(before,await evaluate('({anchor:visibleAnchor(),stable:stableAnchor,resize:resizeAnchor})'))
                await metrics(width);await settled()
                if count==10000 and mode=='offline':
                    y=await evaluate('scrollY')
                    await call('Input.dispatchTouchEvent',dict(type='touchStart',touchPoints=[dict(x=150,y=600,id=1)]))
                    for step in range(1,16):
                        await call('Input.dispatchTouchEvent',dict(type='touchMove',touchPoints=[dict(x=150,y=600-step*20,id=1)]));await asyncio.sleep(.016)
                    await call('Input.dispatchTouchEvent',dict(type='touchEnd',touchPoints=[]))
                    await settled();assert await evaluate('scrollY')>y,(y,await evaluate('({y:scrollY,gesture:scrubGesture,panel:PANEL_DRAG})'))
                    assert await evaluate('scrubIndex===visibleAnchor().id')
                # Fast filter changes during a far jump invalidate old indices/builds.
                await mouse([.99],release=False)
                await evaluate("elQ.value='long';elQ.dispatchEvent(new Event('input'));elQ.value='not-present';elQ.dispatchEvent(new Event('input'));elQ.value='unique-last';elQ.dispatchEvent(new Event('input'));true")
                r=await track();await call('Input.dispatchMouseEvent',dict(type='mouseReleased',button='left',buttons=0,x=r['left'],y=r['y']))
                await settled();assert await evaluate("rowCount()===1 && scrubIndex===0 && elScrubber.getAttribute('aria-disabled')==='true' && rowAt(0).text.includes('unique-last')")
                await evaluate("elQ.value='not-present';elQ.dispatchEvent(new Event('input'));true");await settled()
                assert await evaluate("rowCount()===0 && elScrubber.querySelector('.scrub-time').textContent==='—' && elScrubber.getAttribute('aria-valuenow')==='0'")
                await evaluate("elQ.value='';elU.value='user18';elU.dispatchEvent(new Event('input'));true");await settled()
                n=await evaluate('rowCount()');await mouse([.72]);await verify_target(round(.72*(n-1)))
                assert await evaluate("rowAt(scrubIndex).user.includes('user18')")
                await evaluate("elU.value='';elU.dispatchEvent(new Event('input'));true");await settled()
                assert await evaluate('rowCount()')==count
                # A real mouse selection still crosses an evicted virtual block.
                await mouse([.7]);await settled()
                xy=dict(x=130,y=300)
                await call('Input.dispatchMouseEvent',dict(type='mousePressed',button='left',buttons=1,clickCount=1,**xy))
                for move in range(1,6):
                    await call('Input.dispatchMouseEvent',dict(type='mouseMoved',x=130+move*6,y=300+move*18,buttons=1,button='left'))
                anchor=await evaluate('rememberSelection();selectionRange?.anchor.id');assert anchor is not None
                await evaluate('window.scrollBy(0,150000);true');await settled()
                await call('Input.dispatchMouseEvent',dict(type='mouseMoved',x=160,y=400,buttons=1))
                await call('Input.dispatchMouseEvent',dict(type='mouseReleased',x=160,y=400,button='left',buttons=0,clickCount=1))
                assert await evaluate('selectionRange.anchor.id')==anchor
                assert await evaluate('selectionRange.focus.id')>anchor+800
                # Existing logical selection/copy and real PNG export remain available after jumps.
                await mouse([.7]);await settled()
                copy=await evaluate("(()=>{const n=[...elList.querySelectorAll('.row')],s=getSelection();s.setBaseAndExtent(n[100].firstChild,0,n[120].lastChild,1);rememberSelection();window.__picked=JSON.parse(JSON.stringify(selectionRange));return !!__picked})()")
                assert copy
                await key('Home');await settled();await asyncio.sleep(.03);requests.clear()
                copied=await evaluate("(()=>{const d=new DataTransfer();document.dispatchEvent(new ClipboardEvent('copy',{clipboardData:d,cancelable:true}));return {plain:d.getData('text/plain').split('\\n').length,rich:d.getData('text/html')}})()")
                await asyncio.sleep(.03);await evaluate('true');assert copied['plain']==21 and 'class=' in copied['rich'] and '<script>' not in copied['rich'] and not requests,copied
                assert await evaluate('selectionRange.anchor.id===__picked.anchor.id')
                png=await evaluate('createSnapshotDataUrl(Array.from({length:3},(_,i)=>rowAt(__picked.anchor.id+i)),true,true)')
                assert png.startswith('data:image/png;base64,');assert await evaluate("document.querySelectorAll('.export-wrap').length===0")
                if count==10000 and mode=='offline':(out/f'png-{width}.png').write_bytes(base64.b64decode(png.split(',')[1]))
                # Label center is independent of widths and contrast changes only by clipping.
                if count==10000 and mode=='offline':
                    for f in (0,.25,.5,.75,1):
                        await mouse([f]);await verify_target(round(f*(count-1)))
                        label=await evaluate("(()=>{const r=elScrubber.querySelector('.scrub-track').getBoundingClientRect(),d=elScrubber.querySelector('.scrub-dot').getBoundingClientRect(),a=getComputedStyle(elScrubber.querySelector('.scrub-label')),b=getComputedStyle(elScrubber.querySelector('.scrub-label-on-fill')),t=getComputedStyle(elScrubber.querySelector('.scrub-thumb'));return {delta:d.left+d.width/2-r.left-r.width/2,front:b.color,back:a.color,clip:b.clipPath,opacity:t.opacity,thumb:t.backgroundColor}})()")
                        assert abs(label['delta'])<.1 and label['front']!=label['back'] and label['clip']!='none' and label['opacity']=='1',label
                        await shot(f'compact-{width}-{int(f*100)}')
                assert await evaluate('__maxBuilders')<=1,await evaluate('__maxBuilders')
                assert not errors,errors
                assert await evaluate('window.fixtureXSS===undefined && !scrubActive()')
                result=dict(case='navigation-gestures-filter-copy-png',width=width,count=count,mode=mode,status='PASS',max_rows=await evaluate('__maxRows'),max_builders=await evaluate('__maxBuilders'),seconds=round(time.perf_counter()-start,2))
                results.append(result);(out/'scrubber-results.json').write_text(json.dumps(results,indent=2),encoding='utf-8');print(json.dumps(result),flush=True)
        # Empty payload, singleton, invalid time and touch-only fallback engine.
        for count in (0,1):
            await load(width,count)
            assert await evaluate("elScrubber.getAttribute('aria-disabled')==='true' && elScrubber.getAttribute('aria-valuenow')==='0'")
            await touch([.2,.8]);await key('End');await settled();assert await evaluate('!scrubActive()')
            results.append(dict(case='empty-singleton',width=width,count=count,status='PASS'))
        await load(width,2);await mouse([1]);await verify_target(1)
        await mouse([0]);await verify_target(0)
        results.append(dict(case='short-list',width=width,status='PASS'))
        await load(width,10000)
        await evaluate("rows[0][0]='nonsense';setScrubIndex(0);true")
        assert await evaluate("elScrubber.querySelector('.scrub-time').textContent")== '—'
        assert not errors,errors
        results.append(dict(case='invalid-timestamp',width=width,status='PASS'))
        # Preserve the existing indicator's touch swipe and keyboard activation.
        for dy,expected in ((110,True),(-200,False)):
            r=await evaluate("(()=>{const r=elPanelHandle.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2}})()")
            await call('Input.dispatchTouchEvent',dict(type='touchStart',touchPoints=[dict(**r,id=1)]))
            await call('Input.dispatchTouchEvent',dict(type='touchMove',touchPoints=[dict(x=r['x'],y=r['y']+dy,id=1)]))
            await call('Input.dispatchTouchEvent',dict(type='touchEnd',touchPoints=[]));await settled()
            assert await evaluate('PANEL_EXPANDED')==expected
        await evaluate("window.__handleEvents=[];for(const t of ['click','keydown','keyup'])elPanelHandle.addEventListener(t,e=>__handleEvents.push({type:e.type,key:e.key,detail:e.detail,prevented:e.defaultPrevented}));elPanelHandle.focus();true")
        for typ in ('keyDown','keyUp'):await call('Input.dispatchKeyEvent',dict(type=typ,key='Enter',code='Enter',windowsVirtualKeyCode=13,nativeVirtualKeyCode=13,**({'text':'\r'} if typ=='keyDown' else {})))
        await settled();assert await evaluate('PANEL_EXPANDED'),await evaluate('({events:__handleEvents,active:document.activeElement.outerHTML,suppress:suppressHandleClick})')
    (out/'scrubber-results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
    # Emulate an engine without Pointer Events: native touch events remain real CDP gestures.
    source="delete window.PointerEvent;for(const t of ['pointerdown','pointermove','pointerup','pointercancel'])window.addEventListener(t,e=>e.stopImmediatePropagation(),true);"
    added=await call('Page.addScriptToEvaluateOnNewDocument',dict(source=source))
    try:
        await load(390,10000)
        await touch([.2,.8]);await verify_target(round(.8*9999))
        await touch([.1,.4],cancel=True);await verify_target(round(.4*9999))
        assert not errors,errors
        results.append(dict(case='touch-only-fallback',status='PASS'))
    finally:await call('Page.removeScriptToEvaluateOnNewDocument',dict(identifier=added['identifier']))
    (out/'scrubber-results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
