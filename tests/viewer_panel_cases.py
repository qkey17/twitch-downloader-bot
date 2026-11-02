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
        await asyncio.sleep(.12)
    async def load(width,version='new',count=10000):
        errors.clear();requests.clear()
        await call('Emulation.setDeviceMetricsOverride',dict(width=width,height=900,deviceScaleFactor=1,mobile=width<900))
        await call('Page.navigate',dict(url=(out/f'{version}-{count}-offline.html').as_uri()))
        await wait("document.readyState==='complete' && typeof panelAnimating!=='undefined' && !windowBusy")
        await settled()
    async def shot(name,clip=None):
        params=dict(format='png')
        if clip:params.update(clip=dict(**clip,scale=1),captureBeyondViewport=False)
        data=await call('Page.captureScreenshot',params)
        (out/(name+'.png')).write_bytes(base64.b64decode(data['data']))
    async def panel(expanded):
        await evaluate(f'syncPanelState({str(expanded).lower()});true');await settled()
    def geometry():
        return "JSON.stringify(['header','.panel-top','.panel-stats','.controls','.panel-actions','#list .row'].map(s=>{const r=document.querySelector(s).getBoundingClientRect();return [s,r.x,r.y,r.width,r.height]}))"
    await load(1440,'base');desktop=await evaluate(geometry());await shot('before-desktop-1440')
    await load(1440);assert await evaluate(geometry())==desktop
    await shot('after-desktop-1440');results.append(dict(case='unchanged-desktop-geometry',status='PASS'))
    for width in (320,390):
        await load(width,'base');await shot(f'before-compact-{width}')
        await panel(True);await shot(f'before-expanded-{width}')
        await load(width)
        for expanded in (False,True):
            await panel(expanded)
            # Real layout at all five positions, identical text layers and one
            # rounded clip, with an inset transparent thumb at both endpoints.
            for percent in (0,25,50,75,100):
                await evaluate(f'requestScrub(Math.round({percent}/100*(rowCount()-1)));true');await settled()
                state=await evaluate('''(()=>{const track=elScrubber.querySelector('.scrub-track'),fill=elScrubber.querySelector('.scrub-fill'),thumb=elScrubber.querySelector('.scrub-thumb'),text=elScrubber.querySelector('.scrub-times'),tab=elPanelHandle;
const a=track.getBoundingClientRect(),b=thumb.getBoundingClientRect(),t=text.getBoundingClientRect(),r=tab.getBoundingClientRect(),h=elHeader.getBoundingClientRect();
return {dark:getComputedStyle(track).backgroundColor,light:getComputedStyle(fill).backgroundColor,normal:getComputedStyle(elScrubber.querySelector('.scrub-label')).color,filled:getComputedStyle(elScrubber.querySelector('.scrub-label-on-fill')).color,fill:fill.getBoundingClientRect().width/a.width,thumbAlpha:getComputedStyle(thumb).backgroundColor,thumbLeft:b.left-a.left,thumbRight:a.right-b.right,thumbHeight:b.height,center:t.left+t.width/2-a.left-a.width/2,tab:{x:r.x,y:r.y,width:r.width,height:r.height,ratio:(r.left+r.width/2)/h.width,right:h.right-r.right,font:parseFloat(getComputedStyle(tab).fontSize)},after:getComputedStyle(elHeader,'::after').content,inset:viewportInset(),first:elList.querySelector('.row').getBoundingClientRect().top,rows:elList.querySelectorAll('.row').length};})()''')
                assert state['dark']=='rgb(48, 49, 62)' and state['light']=='rgb(160, 163, 183)',state
                assert state['normal']=='rgb(225, 227, 237)' and state['filled']=='rgb(32, 33, 43)',state
                assert abs(state['center'])<.1 and state['thumbHeight']>28 and state['thumbLeft']>=1.5 and state['thumbRight']>=1.5,state
                assert state['after']=='none' and state['rows']<=2400,state
                tab=state['tab'];assert .68<=tab['ratio']<=.70 and tab['right']>=20 and tab['width']==152 and tab['height']==44 and tab['font']>=14,tab
                if percent==100:assert state['fill']==1,state
                if percent==0:
                    assert state['first']>=state['inset']-.1,state
                    tab_x=tab['x']
                else:assert abs(tab['x']-tab_x)<.1
                await evaluate('document.activeElement?.blur();true')
                await shot(f'after-{"expanded" if expanded else "compact"}-{width}-{percent}')
                if percent in (0,100):
                    clip=await evaluate('({x:0,y:scrollY,width:innerWidth,height:Math.ceil(viewportInset()+10)})')
                    await shot(f'bar-tab-{"expanded" if expanded else "compact"}-{width}-{percent}',clip)
            if expanded:
                await evaluate('requestScrub(0);true');await settled()
                metadata=await evaluate('''(()=>{const d=elStreamDate.querySelector('.meta-value'),range=document.createRange();range.selectNodeContents(d);const rects=[...range.getClientRects()];return {lines:rects.length,text:d.textContent,gaps:[getComputedStyle(elMobileChannel).gap,getComputedStyle(elStreamDate).gap,getComputedStyle(elStat).gap],symbols:[...elHeader.querySelectorAll('.meta-symbol')].map(n=>n.getBoundingClientRect().width),channel:getComputedStyle(elMobileChannel.querySelector('span')).marginLeft,overflow:document.documentElement.scrollWidth>innerWidth};})()''')
                assert metadata['lines']==1 and metadata['text'].endswith(' UTC') and metadata['gaps']==['6px']*3 and metadata['symbols']==[18,18] and metadata['channel']=='0px' and not metadata['overflow'],metadata
                # Keyboard focus uses one visible outline; mobile active/hover
                # never changes geometry or introduces a purple box shadow.
                await call('Input.dispatchKeyEvent',dict(type='keyDown',key='Tab',code='Tab',windowsVirtualKeyCode=9))
                await call('Input.dispatchKeyEvent',dict(type='keyUp',key='Tab',code='Tab',windowsVirtualKeyCode=9))
                for selector in ('#vodCard','#q','#u','#exportBtn','#toggleTime','.panel-handle','#fastScrubber'):
                    focused=await evaluate('''(()=>{const n=document.querySelector('''+json.dumps(selector)+''');n.focus();const s=getComputedStyle(n),r=n.getBoundingClientRect();return {visible:n.matches(':focus-visible'),outline:s.outlineStyle,width:s.outlineWidth,shadow:s.boxShadow,x:r.x,y:r.y,w:r.width,h:r.height,clipped:getComputedStyle(elPanelDetails).overflow};})()''')
                    assert focused['visible'] and focused['outline']=='solid' and focused['width']=='2px' and focused['shadow']=='none', (selector,focused)
                    assert focused['x']>=4 and focused['x']+focused['w']<=width-4 and focused['clipped']=='visible',(selector,focused)
                    if selector in ('#vodCard','#q','#exportBtn'):
                        await shot(f'focus-{selector[1:]}-{width}')
                await shot(f'focus-{width}')
                await evaluate('document.activeElement.blur();true')
                point=await evaluate("(()=>{const r=elExportBtn.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2}})()")
                before=await evaluate("JSON.stringify(elExportBtn.getBoundingClientRect().toJSON())")
                await call('Input.dispatchMouseEvent',dict(type='mousePressed',button='left',buttons=1,**point))
                assert await evaluate("getComputedStyle(elExportBtn).boxShadow==='none' && getComputedStyle(elExportBtn).transform==='none'")
                assert await evaluate("JSON.stringify(elExportBtn.getBoundingClientRect().toJSON())")==before
                await shot(f'active-{width}')
                # Release off-button so no export/dialog is invoked by this style check.
                await call('Input.dispatchMouseEvent',dict(type='mouseReleased',button='left',buttons=0,x=2,y=850))
        await panel(False)
        # All frames retain a cached row relative to the moving tab. Test
        # observations deliberately happen after layout, independent of profiling.
        await evaluate('requestScrub(6000);true');await settled()
        drift=await evaluate('''new Promise(resolve=>{const a=visibleAnchor(),node=elList.querySelector(`.row[data-id="${a.id}"]`),samples=[];
syncPanelState(true);function sample(){const r=node.getBoundingClientRect();samples.push({id:visibleAnchor()?.id,error:Math.abs(r.top-viewportInset()-(a.top-a.inset))});if(panelAnimating)requestAnimationFrame(()=>setTimeout(sample,0));else resolve({a,samples});}requestAnimationFrame(()=>setTimeout(sample,0));})''')
        assert all(s['id']==drift['a']['id'] and s['error']<3 for s in drift['samples']),drift
        await evaluate('syncPanelState(false);setTimeout(()=>syncPanelState(true),40);setTimeout(()=>syncPanelState(false),85);setTimeout(()=>syncPanelState(true),130);true');await settled()
        assert await evaluate("PANEL_EXPANDED && elPanelHandle.getAttribute('aria-expanded')==='true' && elPanelHandle.querySelector('span').textContent==='Свернуть' && elPanelDetails.style.height==='auto'")
        await call('Emulation.setEmulatedMedia',dict(features=[dict(name='prefers-reduced-motion',value='reduce')]))
        await panel(False);assert not await evaluate('panelAnimating')
        await call('Emulation.setEmulatedMedia',dict(features=[]))
        assert not errors,errors
        results.append(dict(case='mobile-visual-focus-animation',width=width,status='PASS',max_anchor_error_px=max(s['error'] for s in drift['samples'])))
    (out/'panel-visual-results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
