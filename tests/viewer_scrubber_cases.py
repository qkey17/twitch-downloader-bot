"""Fast-scrubber cases using viewer_window_browser's existing CDP transport/fixtures."""
import asyncio,base64,json,time

async def check_scrubber(call,evaluate,wait,out,args,errors,requests):
    results=[]
    async def settled():
        await wait('!FILTERING && !panelAnimating && !windowBusy && !scrubActive() && !scrubFrame && !scrubSyncFrame')
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
        return await evaluate("(()=>{const r=elScrubber.getBoundingClientRect();const t=elScrubber.querySelector('.scrub-track').getBoundingClientRect();return {left:t.left,width:t.width,y:r.top+r.height/2}})()")
    async def gesture(fractions,touching=False,release=True,cancel=False):
        r=await track();initial=await evaluate('scrubPercent(scrubIndex,rowCount())/100');count=await evaluate('rowCount()')
        # Aim at the center of a logical row, avoiding half-index ties caused
        # solely by CDP's finite coordinate precision on large datasets.
        fractions=[round(f*(count-1))/(count-1) if count>1 else 0 for f in fractions]
        # Fractions describe requested logical positions. The actual input is a
        # relative drag, beginning anywhere in the hit area, never a track click.
        start=r['left'] if fractions[-1]>=initial else r['left']+r['width']
        def pt(f):return dict(x=start+(f-initial)*r['width'],y=r['y'])
        if touching:
            await call('Input.dispatchTouchEvent',dict(type='touchStart',touchPoints=[dict(x=start,y=r['y'],id=1)]))
            for f in fractions:
                await call('Input.dispatchTouchEvent',dict(type='touchMove',touchPoints=[dict(**pt(f),id=1)]));await asyncio.sleep(.012)
            await call('Input.dispatchTouchEvent',dict(type='touchCancel' if cancel else 'touchEnd',touchPoints=[]))
        else:
            await call('Input.dispatchMouseEvent',dict(type='mousePressed',button='left',buttons=1,clickCount=1,x=start,y=r['y']))
            for f in fractions:
                await call('Input.dispatchMouseEvent',dict(type='mouseMoved',button='left',buttons=1,**pt(f)));await asyncio.sleep(.006)
            if release:await call('Input.dispatchMouseEvent',dict(type='mouseReleased',button='left',buttons=0,clickCount=1,**pt(fractions[-1])))
        return pt(fractions[-1])
    async def mouse(fractions,release=True):return await gesture(fractions,release=release)
    async def touch(fractions,cancel=False):return await gesture(fractions,touching=True,cancel=cancel)
    async def verify_target(index):
        await settled()
        state=await evaluate('''(()=>{const id='''+str(index)+''',n=elList.querySelector(`.row[data-id="${id}"]`),r=n?.getBoundingClientRect();return {id:scrubIndex,top:r?.top??null,bottom:r?.bottom??null,inset:viewportInset(),rows:elList.querySelectorAll('.row').length,visible:visibleAnchor(),max:window.__maxRows||0,active:scrubActive(),busy:windowBusy,pct:elScrubber.getAttribute('aria-valuenow'),time:elScrubber.querySelector('.scrub-times').textContent,expected:((physicalChatEnd() && durationTime(meta.duration_seconds)!=='—')?durationTime(meta.duration_seconds):scrubTime(rowAt(id).t))+' / '+durationTime(meta.duration_seconds)}})()''')
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
    assert await evaluate("getComputedStyle(document.querySelector('.mobile-control-row')).display==='none'")
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
                assert await evaluate("elHeader.offsetHeight<=70 && elPanelDetails.offsetHeight===0 && elPanelDetails.inert")
                assert await evaluate("elList.querySelector('.row').getBoundingClientRect().top>=elPanelHandle.getBoundingClientRect().bottom && elScrubber.offsetHeight>=44 && document.documentElement.scrollWidth===innerWidth")
                start=time.perf_counter()
                for f in (1,0,.1,.9,.37,.75,.25,.5):
                    await mouse([f]);await verify_target(round(f*(count-1)))
                await mouse([.2,.9999]);near_end=round(.9999*(count-1));await verify_target(near_end)
                await panel();await verify_target(near_end);await panel();await verify_target(near_end)
                await mouse([.5]);await settled()
                if count==10000 and mode=='offline':
                    # Down at 20% while thumb is at 80% must be a true no-op.
                    await mouse([.8]);await settled();r=await track()
                    before_tap=await evaluate('({id:scrubIndex,y:scrollY})')
                    point=dict(x=r['left']+.2*r['width'],y=r['y'])
                    await call('Input.dispatchMouseEvent',dict(type='mousePressed',button='left',buttons=1,**point))
                    assert await evaluate('scrubIndex')==before_tap['id']
                    await call('Input.dispatchMouseEvent',dict(type='mouseReleased',button='left',buttons=0,**point));await settled()
                    assert await evaluate('({id:scrubIndex,y:scrollY})')==before_tap
                    # At either endpoint a gesture can start in the middle of the screen.
                    for initial,delta in ((0,50),(1,-50)):
                        await mouse([initial]);await settled();r=await track();base=await evaluate('scrubIndex')
                        point=dict(x=r['left']+r['width']/2,y=r['y'])
                        await call('Input.dispatchTouchEvent',dict(type='touchStart',touchPoints=[dict(**point,id=1)]))
                        assert await evaluate('scrubIndex')==base
                        await call('Input.dispatchTouchEvent',dict(type='touchMove',touchPoints=[dict(x=point['x']+delta,y=point['y'],id=1)]))
                        await call('Input.dispatchTouchEvent',dict(type='touchEnd',touchPoints=[]))
                        target=round(max(0,min(count-1,base+delta/r['width']*(count-1))))
                        await verify_target(target)
                    # End is physical, not just a visible last row; filters never substitute duration.
                    await key('End');await settled()
                    assert await evaluate("elScrubber.querySelector('.scrub-times').textContent==='120:51:28 / 120:51:28'")
                    await evaluate('window.scrollBy(0,-2);true');await settled()
                    assert await evaluate("elScrubber.querySelector('.scrub-times').textContent.startsWith('120:51:28 /')")
                    await evaluate('window.scrollBy(0,-6);true');await settled()
                    assert await evaluate("!elScrubber.querySelector('.scrub-times').textContent.startsWith('120:51:28 /')")
                    await evaluate("elQ.value='unique-last';elQ.dispatchEvent(new Event('input'));true");await settled()
                    assert await evaluate("elScrubber.querySelector('.scrub-times').textContent===scrubTime(rowAt(0).t)+' / 120:51:28'")
                    await evaluate("elQ.value='';elQ.dispatchEvent(new Event('input'));true");await settled()
                    await evaluate("rows[0][0]='105:42:15';setScrubIndex(0);true")
                    assert await evaluate("elScrubber.querySelector('.scrub-times').textContent==='105:42:15 / 120:51:28'")
                    bounds=await evaluate("(()=>{const a=elScrubber.querySelector('.scrub-track').getBoundingClientRect(),b=elScrubber.querySelector('.scrub-times').getBoundingClientRect();return {center:Math.abs(b.left+b.width/2-a.left-a.width/2),width:b.width,available:a.width}})()")
                    assert bounds['center']<.1 and bounds['width']<=bounds['available'],bounds
                    await shot(f'long-hours-{width}')
                    await evaluate('meta.duration_seconds=null;setScrubIndex(0);true')
                    assert await evaluate("elScrubber.querySelector('.scrub-times').textContent==='105:42:15 / —'")
                    await evaluate("meta.duration_seconds=435088;rows[0][0]='00:00:00';true")
                await mouse([.5]);await settled()
                # Real continuous drag with reversals; current held value cannot be overwritten by scroll.
                end_point=await mouse([.5,.8,.2,.95,.05,.8,.63],release=False)
                held=await evaluate('scrubIndex');await evaluate('window.scrollBy(0,100);true');await asyncio.sleep(.05)
                assert await evaluate('scrubIndex')==held
                r=await track();await call('Input.dispatchMouseEvent',dict(type='mouseReleased',button='left',buttons=0,clickCount=1,**end_point))
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
                await evaluate('window.__panelSamples=[];window.__panelSampling=true;function samplePanel(){if(!__panelSampling)return;const a=visibleAnchor();if(a)__panelSamples.push(a);requestAnimationFrame(()=>setTimeout(samplePanel,0));}samplePanel();true')
                await panel();assert await evaluate('PANEL_EXPANDED')
                samples=await evaluate('__panelSampling=false;__panelSamples')
                assert all(x['id']==before['id'] and abs((x['top']-x['inset'])-(before['top']-before['inset']))<3 for x in samples),(before,samples)
                # Rapid reversals keep one animation transaction and the same logical anchor.
                await evaluate('syncPanelState(false);setTimeout(()=>syncPanelState(true),50);setTimeout(()=>syncPanelState(false),100);true');await settled()
                await panel();assert await evaluate('PANEL_EXPANDED')
                after=await evaluate('visibleAnchor()')
                assert before['id']==after['id'] and abs((before['top']-before['inset'])-(after['top']-after['inset']))<3,(before,after)
                assert await evaluate('scrubIndex')==index
                await mouse([.8]);await verify_target(round(.8*(count-1)))
                if count==10000 and mode=='offline':
                    for f in (0,.25,.5,.75,1):
                        await mouse([f]);await verify_target(round(f*(count-1)))
                        assert await evaluate("(()=>{const r=elScrubber.querySelector('.scrub-track').getBoundingClientRect(),d=elScrubber.querySelector('.scrub-times').getBoundingClientRect();return Math.abs(d.left+d.width/2-r.left-r.width/2)<.1})()")
                        await shot(f'expanded-{width}-{int(f*100)}')
                    await mouse([.8]);await settled()
                    prior=await evaluate('visibleAnchor()')
                    await metrics(390 if width==320 else 320);await settled();assert await evaluate('visibleAnchor().id')==prior['id']
                    await metrics(width);await settled()
                    await evaluate("elTitle.textContent='Very long title '.repeat(20);fitVodTitle();true");await settled()
                    assert await evaluate('visibleAnchor().id')==prior['id']
                    await evaluate("elTitle.textContent='Synthetic title';fitVodTitle();true");await settled()
                    await shot(f'expanded-{width}')
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
                assert await evaluate("rowCount()===0 && elScrubber.querySelector('.scrub-times').textContent==='— / 120:51:28' && elScrubber.getAttribute('aria-valuenow')==='0'")
                await evaluate("elQ.value='';elU.value='user18';elU.dispatchEvent(new Event('input'));true");await settled()
                n=await evaluate('rowCount()');await mouse([.72]);await verify_target(round(.72*(n-1)))
                assert await evaluate("rowAt(scrubIndex).user.includes('user18')")
                saved_filter=await evaluate('({u:elU.value,n:rowCount(),id:scrubIndex})')
                await panel();await panel()
                assert await evaluate('({u:elU.value,n:rowCount(),id:scrubIndex})')==saved_filter
                await evaluate("elU.value='';elU.dispatchEvent(new Event('input'));true");await settled()
                assert await evaluate('rowCount()')==count
                # Time settings survive toggles; selection is tested independently below.
                await panel();await evaluate('elToggleTime.click();true');await settled()
                assert not await evaluate('SHOW_TIME')
                await panel();await panel();assert not await evaluate('SHOW_TIME')
                await evaluate('elToggleTime.click();true');await settled();await panel()
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
                saved_selection=await evaluate('JSON.stringify(selectionRange)')
                await panel();await panel()
                assert await evaluate('JSON.stringify(selectionRange)')==saved_selection
                await key('Home');await settled();await asyncio.sleep(.03);requests.clear()
                copied=await evaluate("(()=>{const d=new DataTransfer();document.dispatchEvent(new ClipboardEvent('copy',{clipboardData:d,cancelable:true}));return {plain:d.getData('text/plain').split('\\n').length,rich:d.getData('text/html')}})()")
                await asyncio.sleep(.03);await evaluate('true');assert copied['plain']==21 and 'class=' in copied['rich'] and '<script>' not in copied['rich'] and not requests,copied
                assert await evaluate('selectionRange.anchor.id===__picked.anchor.id')
                png=await evaluate('createSnapshotDataUrl(Array.from({length:3},(_,i)=>rowAt(__picked.anchor.id+i)),true,true)')
                assert png.startswith('data:image/png;base64,');assert await evaluate("document.querySelectorAll('.export-wrap').length===0")
                if count==10000 and mode=='offline':(out/f'png-{width}.png').write_bytes(base64.b64decode(png.split(',')[1]))
                # Whole time line stays centered on both identical contrast layers.
                if count==10000 and mode=='offline':
                    for f in (0,.25,.5,.75,1):
                        await mouse([f]);await verify_target(round(f*(count-1)))
                        label=await evaluate("(()=>{const r=elScrubber.querySelector('.scrub-track').getBoundingClientRect(),d=elScrubber.querySelector('.scrub-times').getBoundingClientRect(),a=getComputedStyle(elScrubber.querySelector('.scrub-label')),t=getComputedStyle(elScrubber.querySelector('.scrub-thumb'));return {delta:d.left+d.width/2-r.left-r.width/2,width:d.width,available:r.width,front:a.backgroundImage,back:a.color,clip:a.backgroundClip,background:t.backgroundColor,height:parseFloat(t.height)}})()")
                        assert abs(label['delta'])<.1 and label['width']<=label['available'] and label['front']!=label['back'] and label['clip']!='none' and label['height']>28,label
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
        await load(width,10000)
        await call('Emulation.setEmulatedMedia',dict(features=[dict(name='prefers-reduced-motion',value='reduce')]))
        await evaluate('syncPanelState(true);true');assert not await evaluate('panelAnimating');await settled()
        await panel();assert not await evaluate('PANEL_EXPANDED')
        await call('Emulation.setEmulatedMedia',dict(features=[]))
        await load(width,2);await mouse([1]);await verify_target(1)
        await mouse([0]);await verify_target(0)
        results.append(dict(case='short-list',width=width,status='PASS'))
        await load(width,10000)
        await evaluate("rows[0][0]='nonsense';setScrubIndex(0);true")
        assert await evaluate("elScrubber.querySelector('.scrub-times').textContent")== '— / 120:51:28'
        assert not errors,errors
        results.append(dict(case='invalid-timestamp',width=width,status='PASS'))
        # Vertical swipes must never change state; only the tab click toggles it.
        for dy in (110,-110):
            r=await evaluate("(()=>{const r=elHeader.getBoundingClientRect();return {x:r.left+4,y:r.bottom-6}})()")
            await call('Input.dispatchTouchEvent',dict(type='touchStart',touchPoints=[dict(**r,id=1)]))
            await call('Input.dispatchTouchEvent',dict(type='touchMove',touchPoints=[dict(x=r['x'],y=r['y']+dy,id=1)]))
            await call('Input.dispatchTouchEvent',dict(type='touchEnd',touchPoints=[]));await settled()
            assert not await evaluate('PANEL_EXPANDED')
        await evaluate('elPanelHandle.focus();true')
        for typ in ('keyDown','keyUp'):await call('Input.dispatchKeyEvent',dict(type=typ,key='Enter',code='Enter',windowsVirtualKeyCode=13,**({'text':'\r'} if typ=='keyDown' else {})))
        await settled();assert await evaluate('PANEL_EXPANDED')
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
