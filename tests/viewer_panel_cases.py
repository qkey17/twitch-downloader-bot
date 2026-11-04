"""Focused synthetic mobile panel checks on the existing CDP transport."""
import asyncio
import base64
import json
import statistics


async def check_panel(call, evaluate, wait, out, args, errors, requests):
    profiles = []
    await call('Performance.enable')
    await call('Emulation.setDeviceMetricsOverride', dict(width=390, height=900, deviceScaleFactor=1, mobile=True))
    for version in ('base', 'new'):
        await call('Page.navigate', dict(url=(out / f'{version}-100000-offline.html').as_uri()))
        await wait("document.readyState==='complete' && typeof scrubActive==='function' && !windowBusy && !panelAnimating")
        await evaluate('requestScrub(60000);true')
        await wait('!windowBusy && !scrubActive() && !scrubSyncFrame')
        await asyncio.sleep(.15)
        await evaluate('''window.__calls={rect:0,measure:0,visible:0,geometry:0};
const rect=Element.prototype.getBoundingClientRect;
Element.prototype.getBoundingClientRect=function(...a){__calls.rect++;return rect.apply(this,a)};
for(const [name,key] of [['measureBlocks','measure'],['visibleAnchor','visible'],['updatePanelGeometry','geometry']]){
  const original=window[name];window[name]=(...a)=>{__calls[key]++;return original(...a)};
}true''')
        for throttle,expanded in ((1,True),(1,False),(4,True),(4,False)):
            await call('Emulation.setCPUThrottlingRate', dict(rate=throttle))
            before = {m['name']: m['value'] for m in (await call('Performance.getMetrics'))['metrics']}
            animation = await evaluate('''new Promise(resolve=>{
window.__calls={rect:0,measure:0,visible:0,geometry:0};longTasks=[];
const frames=[],start=performance.now();let last=null;
syncPanelState(''' + str(expanded).lower() + ''');
function sample(now){if(last!==null)frames.push(now-last);last=now;
if(panelAnimating)requestAnimationFrame(sample);
else resolve({elapsed_ms:performance.now()-start,frames_ms:frames,calls:{...__calls},long_tasks_ms:longTasks.slice()});
}requestAnimationFrame(sample);
})''')
            after = {m['name']: m['value'] for m in (await call('Performance.getMetrics'))['metrics']}
            frames=sorted(animation['frames_ms'])
            animation.update(version=version, expanded=expanded, cpu_throttle=throttle, count=100000,
                             frame_median_ms=statistics.median(frames),frame_max_ms=max(frames),
                             layout_count=after['LayoutCount']-before['LayoutCount'],
                             layout_ms=1000*(after['LayoutDuration']-before['LayoutDuration']),
                             style_ms=1000*(after['RecalcStyleDuration']-before['RecalcStyleDuration']),
                             script_ms=1000*(after['ScriptDuration']-before['ScriptDuration']))
            profiles.append(animation)
            print(json.dumps(animation), flush=True)
            await asyncio.sleep(.1)
        await call('Emulation.setCPUThrottlingRate', dict(rate=1))
        assert not errors, errors
    (out / 'panel-profile.json').write_text(json.dumps(profiles, indent=2), encoding='utf-8')
    await visual_checks(call,evaluate,wait,out,errors,requests)


async def visual_checks(call,evaluate,wait,out,errors,requests):
    results=[]
    async def settled():
        await wait('!panelAnimating && !windowBusy && !scrubActive() && !scrubSyncFrame')
        await asyncio.sleep(.15)
    async def size(width):
        await call('Emulation.setDeviceMetricsOverride',dict(width=width,height=900,deviceScaleFactor=1,mobile=width<900))
        await call('Emulation.setTouchEmulationEnabled',dict(enabled=width<900,maxTouchPoints=1))
        if await evaluate("typeof panelAnimating!=='undefined'"):await settled()
    async def load(width,count=10000,version='new'):
        errors.clear();await size(width)
        await call('Page.navigate',dict(url=(out/f'{version}-{count}-offline.html').as_uri()))
        await wait("document.readyState==='complete' && typeof panelAnimating!=='undefined' && !windowBusy")
        await settled()
    async def shot(name,strip=False):
        params=dict(format='png')
        if strip:
            params.update(clip=await evaluate('({x:0,y:scrollY,width:innerWidth,height:Math.ceil(viewportInset()+10),scale:1})'),captureBeyondViewport=False)
        data=await call('Page.captureScreenshot',params)
        (out/(name+'.png')).write_bytes(base64.b64decode(data['data']))
    async def toggle():
        p=await evaluate('(()=>{const r=elPanelHandle.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2}})()')
        for typ in ('mousePressed','mouseReleased'):
            await call('Input.dispatchMouseEvent',dict(type=typ,button='left',buttons=1 if typ=='mousePressed' else 0,clickCount=1,**p))
        await settled()
    async def consistent(expanded):
        assert await evaluate(f"PANEL_EXPANDED==={str(expanded).lower()} && elPanelHandle.getAttribute('aria-expanded')==='{str(expanded).lower()}' && elPanelDetails.inert==={str(not expanded).lower()} && (elPanelDetails.getBoundingClientRect().height>0)==={str(expanded).lower()}")
    geom="JSON.stringify(['header','.panel-top','.panel-stats','.controls','.panel-actions','#list .row'].map(s=>{const r=document.querySelector(s).getBoundingClientRect();return [s,r.x,r.y,r.width,r.height]}))"
    await load(1440,version='base');before=await evaluate(geom)
    await load(1440);assert await evaluate(geom)==before
    await shot('desktop-1440');results.append(dict(case='desktop-geometry',status='PASS'))
    for width in (320,390):
        await load(width);await consistent(False)
        assert await evaluate("document.querySelectorAll('#fastScrubber').length===1 && !document.querySelector('.panel-handle') && elPanelHandle.textContent.trim()==='' && elList.querySelector('.row').getBoundingClientRect().top>=viewportInset() && elList.querySelector('.row').getBoundingClientRect().top-viewportInset()<2")
        for expanded in (False,True):
            if expanded:await toggle()
            await consistent(expanded)
            dims=await evaluate('''(()=>{const r=n=>{const a=n.getBoundingClientRect();return {x:a.x,y:a.y,w:a.width,h:a.height,right:a.right}};return {q:r(elQ),u:r(elU),a:r(elExportBtn),b:r(elToggleTime),toggle:r(elPanelHandle),scrub:r(elScrubber),thumb:r(elScrubber.querySelector('.scrub-thumb')),date:r(elStreamDate),stat:r(elStat),header:r(elHeader),overflow:document.documentElement.scrollWidth>innerWidth};})()''')
            assert not dims['overflow'] and dims['toggle']['w']==dims['toggle']['h']==dims['scrub']['h']==46,dims
            assert abs(dims['thumb']['y']-dims['toggle']['y'])<.1 and dims['thumb']['h']==dims['toggle']['h'],dims
            assert abs(dims['toggle']['right']-(width-24))<.1,dims
            if expanded:
                assert all(dims[k]['h']==46 for k in ('q','u','a','b')),dims
                assert abs(dims['q']['w']-dims['u']['w'])<.1 and abs(dims['a']['w']-dims['b']['w'])<.1,dims
                assert dims['q']['x']==dims['a']['x'] and dims['u']['x']==dims['b']['x'] and abs(dims['q']['w']-dims['a']['w'])<.1,dims
                assert abs(dims['stat']['right']-(width-24))<.1,dims
                assert await evaluate("(()=>{const r=document.createRange();r.selectNodeContents(elStreamDate.querySelector('.meta-value'));return r.getClientRects().length===1;})()")
            await shot(f'{"expanded" if expanded else "compact"}-{width}')
            await shot(f'controls-{"expanded" if expanded else "compact"}-{width}',True)
            for percent in (0,25,50,75,100):
                await evaluate(f'requestScrub(Math.round({percent}/100*(rowCount()-1)));true');await settled()
                # The opaque capsule stays above the shortened track and below
                # the single contrast-colored glyph mask.
                paint=await evaluate('''(()=>{const t=elScrubber.querySelector('.scrub-thumb'),a=t.getBoundingClientRect(),r=elScrubber.querySelector('.scrub-track').getBoundingClientRect(),fill=elScrubber.querySelector('.scrub-fill').getBoundingClientRect();const s=getComputedStyle(t);return {split:fill.right-a.left,background:s.backgroundImage,origin:s.backgroundOrigin,clip:s.backgroundClip,center:a.y+a.height/2-r.y-r.height/2,labelZ:getComputedStyle(elScrubber.querySelector('.scrub-label')).zIndex,thumbZ:s.zIndex};})()''')
                assert paint['background']=='none' and paint['origin']=='border-box' and paint['clip']=='border-box' and abs(paint['center'])<.1 and int(paint['labelZ'])>int(paint['thumbZ']),paint
                await shot(f'bar-{width}-{int(expanded)}-{percent}',True)
            await evaluate('requestScrub(0);true');await settled()
        # Mode crossings, not only resize, reset mobile compact. Width changes
        # within mobile preserve intent; first real click always toggles it.
        await size(1440);await size(width);await consistent(False)
        await toggle();await consistent(True)
        await size(390 if width==320 else 320);await consistent(True)
        await size(width);await consistent(True)
        await evaluate('syncPanelState(false);setTimeout(()=>syncPanelState(true),40);setTimeout(()=>syncPanelState(false),85);true');await settled();await consistent(False)
        await evaluate('requestScrub(6000);true');await settled();anchor=await evaluate('visibleAnchor()')
        await toggle();after=await evaluate('visibleAnchor()')
        assert after['id']==anchor['id'] and abs((after['top']-after['inset'])-(anchor['top']-anchor['inset']))<3,(anchor,after)
        await call('Emulation.setEmulatedMedia',dict(features=[dict(name='prefers-reduced-motion',value='reduce')]))
        await evaluate('syncPanelState(false);true');assert not await evaluate('panelAnimating');await settled()
        await call('Emulation.setEmulatedMedia',dict(features=[]))
        # Exact owner's reversal examples, real pointer and native touch.
        for touch in (False,True):
            for right in (False,True):
                await evaluate(f'requestScrub(Math.round({.8 if right else .2}*(rowCount()-1)));true');await settled()
                r=await evaluate('(()=>{const r=elScrubber.getBoundingClientRect();return {x:r.x,y:r.y+r.height/2,w:r.width-22};})()')
                start=.2 if right else .8
                async def move(typ,f):
                    p=dict(x=r['x']+11+r['w']*f,y=r['y'])
                    if touch:await call('Input.dispatchTouchEvent',dict(type=typ,touchPoints=[] if typ in ('touchEnd','touchCancel') else [dict(**p,id=1)]))
                    else:await call('Input.dispatchMouseEvent',dict(type=typ,button='left',buttons=0 if typ=='mouseReleased' else 1,**p))
                    # Native touchmove delivery may coalesce within one frame.
                    await asyncio.sleep(.03)
                initial=await evaluate('scrubIndex');await move('touchStart' if touch else 'mousePressed',start)
                assert await evaluate('scrubIndex')==initial
                for f,target in zip(([.4,.6,.5,.4] if right else [.6,.4,.5,.6]),([1,1,.9,.8] if right else [0,0,.1,.2])):
                    await move('touchMove' if touch else 'mouseMoved',f)
                    assert abs(await evaluate('scrubIndex')-round(target*9999))<=1,(width,touch,right,f,target,await evaluate('({index:scrubIndex,gesture:scrubGesture})'),r)
                await move('touchCancel' if touch else 'mouseReleased',f);await settled()
        assert not errors,errors
        results.append(dict(case='control-row-geometry-modes-endpoint-reversal',width=width,status='PASS'))
    (out/'control-row-results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')


async def check_message_clearance(call,evaluate,wait,out,args,errors,requests):
    """Focused gap/anchor regression; no gesture matrix or profiling."""
    results=[]
    async def settled():
        await wait('!panelAnimating && !windowBusy && !scrubActive() && !scrubSyncFrame')
        await asyncio.sleep(.12)
    async def load(width,version):
        await call('Emulation.setDeviceMetricsOverride',dict(width=width,height=900,deviceScaleFactor=1,mobile=width<900))
        await call('Page.navigate',dict(url=(out/f'{version}-10000-offline.html').as_uri()))
        await wait("document.readyState==='complete' && typeof panelAnimating!=='undefined' && !windowBusy")
        await settled()
    async def shot(name):
        data=await call('Page.captureScreenshot',dict(format='png'))
        (out/(name+'.png')).write_bytes(base64.b64decode(data['data']))
    geometry="JSON.stringify(['header','.controls','.panel-actions','#list .row'].map(s=>{const r=document.querySelector(s).getBoundingClientRect();return [r.x,r.y,r.width,r.height]}))"
    await load(1440,'base');desktop=await evaluate(geometry)
    await load(1440,'new');assert await evaluate(geometry)==desktop
    results.append(dict(case='desktop-unchanged',status='PASS'))
    for width in (320,390):
        for expanded in (False,True):
            old_height=None
            for version in ('base','new'):
                await load(width,version)
                if expanded:await evaluate('elPanelHandle.click();true');await settled()
                state=await evaluate('({gap:elList.querySelector(".row").getBoundingClientRect().top-elHeader.getBoundingClientRect().bottom,height:elHeader.getBoundingClientRect().height})')
                if version=='base':
                    assert abs(state['gap']-6)<.1,state
                    old_height=state['height']
                else:assert 0<=state['gap']<=2 and state['height']==old_height,state
                await shot(f'{version}-{"expanded" if expanded else "compact"}-{width}')
            for index in (0,5000,9999,2000,0):
                await evaluate(f'requestScrub({index});true');await settled()
                target=await evaluate(f'(()=>{{const r=elList.querySelector(`.row[data-id="{index}"]`).getBoundingClientRect();return {{gap:r.top-elHeader.getBoundingClientRect().bottom,bottom:r.bottom,inset:viewportInset()}};}})()')
                assert target['gap']>=-.5 and target['bottom']>target['inset'],target
                if index in (0,5000,2000):assert target['gap']<=2,target
            await evaluate('window.scrollBy(0,251);true');await settled()
            anchor=await evaluate('visibleAnchor()');assert anchor
            await evaluate('elPanelHandle.click();true');await settled()
            after=await evaluate('visibleAnchor()')
            assert anchor['id']==after['id'] and abs((anchor['top']-anchor['inset'])-(after['top']-after['inset']))<3,(anchor,after)
            assert not errors,errors
            results.append(dict(width=width,expanded=expanded,before_gap=6,after_gap=state['gap'],panel_height_unchanged=True,scroll_and_scrubber='PASS',status='PASS'))
    (out/'clearance-results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
    print(json.dumps(results),flush=True)
