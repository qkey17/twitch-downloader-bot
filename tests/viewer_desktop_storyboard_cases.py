"""Offline Chromium coverage for message navigation and the preview panels."""
import json
from statistics import median


async def check_unified_preview(call,evaluate,wait,out,args,errors,requests):
    count=args.sizes[0]
    await call('Network.enable')
    await call('Network.emulateNetworkConditions',dict(offline=True,latency=0,downloadThroughput=0,uploadThroughput=0))

    async def resize(width,height=900,scale=1):
        await call('Emulation.setDeviceMetricsOverride',dict(
            width=width,height=height,deviceScaleFactor=scale,mobile=width<600))
        await call('Emulation.setTouchEmulationEnabled',dict(enabled=width<600,maxTouchPoints=1))

    async def load(version,width,height=900):
        await resize(width,height)
        await call('Page.navigate',dict(url=(out/f'{version}-{count}-offline.html').as_uri()))
        await wait(f'document.readyState==="complete" && innerWidth==={width} && !windowBusy && rowCount()>1')

    async def motion(value):
        await call('Emulation.setEmulatedMedia',dict(features=[dict(name='prefers-reduced-motion',value=value)]))

    async def expand_mobile():
        await evaluate('elPanelHandle.click();true')
        await wait('PANEL_EXPANDED && !panelAnimating')

    async def mouse(event,point):
        extras=dict(button='left',clickCount=1) if event!='mouseMoved' else {}
        await call('Input.dispatchMouseEvent',dict(type=event,x=point[0],y=point[1],**extras))

    ready='elVodPreviewImg.complete && elVodPreviewImg.naturalWidth'
    await motion('no-preference')
    for width in (390,901,1024,1440):
        for version,pixels in (('no-preview',0),('no-storyboard',1280),('new',220)):
            await load(version,width)
            if width<901:
                await expand_mobile()
            await wait(f'{ready}==={pixels}')
            assert await evaluate("""elVodLink.textContent==='🔗 Ссылка' && !elVodLink.hidden
              && document.documentElement.scrollWidth<=innerWidth
              && document.querySelectorAll('#vodPreviewImg').length===1""")
            if width>=901:
                assert await evaluate("""(()=>{
                  const h=elHeader.getBoundingClientRect(),l=elList.getBoundingClientRect();
                  return h.width===336 && Math.abs(innerHeight-h.bottom-24)<1
                    && Math.abs(h.left-(document.documentElement.clientWidth-l.right))<2
                    && h.right<l.left;
                })()""")
                assert await evaluate(f'elDesktopPreview.hidden==={str(not pixels).lower()}')
                if pixels:
                    assert await evaluate("""elVodCard.parentElement===elDesktopPreview &&
                      elPanelDetails.getBoundingClientRect().bottom<=elDesktopPreview.getBoundingClientRect().top &&
                      Math.abs(elPreviewHome.clientWidth/elPreviewHome.clientHeight-16/9)<.025""")
                    await evaluate('elVodCard.focus();elVodCard.click();true')
                    assert await evaluate("""desktopPreviewCollapsed && elVodCard.hidden
                      && document.activeElement===elDesktopPreviewToggle && elDesktopPreview.getAnimations().length===1
                      && elDesktopPreview.getAnimations()[0].effect.getTiming().duration===250""")
                    await evaluate('elDesktopPreviewToggle.click();elVodCard.click();elDesktopPreviewToggle.click();true')
                    await wait('!desktopPreviewCollapsed && !elDesktopPreview.getAnimations().length')
                    assert await evaluate('!elVodCard.hidden && elDesktopPreviewToggle.hidden')
            elif pixels:
                assert await evaluate("elVodCard.parentElement===elMobilePreviewHome && !elPanelDetails.inert")
                await evaluate('elVodCard.click();true')
                await wait('storyboardMode && !finishPreviewFlight')
                assert await evaluate('elVodPreviewImg.parentElement===elMobilePreviewMedia && elScrubber.clientWidth>0')
                await evaluate('elPanelHandle.click();true')
                await wait('!storyboardMode && !finishPreviewFlight')
            else:
                assert await evaluate('elVodCard.hidden && elTitle.clientWidth>=elHeader.clientWidth-50')

    await load('new',1024,320)
    await resize(1024,320,1.25)
    await wait('devicePixelRatio===1.25')
    await evaluate("elTitle.textContent='Long title '.repeat(100);true")
    assert await evaluate("""elPanelDetails.scrollHeight>elPanelDetails.clientHeight &&
      elHeader.getBoundingClientRect().bottom<=innerHeight-23""")
    before=await evaluate('elDesktopPreview.getBoundingClientRect().toJSON()')
    await evaluate('elVodLink.focus();elPanelDetails.scrollTop=elPanelDetails.scrollHeight;true')
    assert await evaluate('elPanelDetails.scrollTop>0 && document.activeElement===elVodLink')
    assert await evaluate('elDesktopPreview.getBoundingClientRect().toJSON()')==before
    await motion('reduce')
    await evaluate('elVodCard.click();true')
    assert await evaluate('desktopPreviewCollapsed && !elDesktopPreview.getAnimations().length')
    await resize(900)
    await wait('MOBILE_MQ.matches && elDesktopPreview.hidden && !PANEL_EXPANDED')
    await resize(901)
    await wait('!MOBILE_MQ.matches && !elDesktopPreview.hidden && desktopPreviewCollapsed')
    await motion('no-preference')

    # Independent DOM oracle. Restrict the search to actually visible rows.
    visible="""(()=>{
      const l=elList.getBoundingClientRect();
      const top=Math.max(0,l.top,viewportInset()),bottom=Math.min(innerHeight,l.bottom);
      return [...elList.querySelectorAll('.row')].map(n=>[+n.dataset.id,n.getBoundingClientRect()])
        .filter(([,r])=>r.bottom>top && r.top<bottom);
    })()"""
    automatic=f'({visible}).filter(([,r])=>r.top>=0 && r.bottom<=innerHeight).at(-1)?.[0]'
    selection_matches=f'desktopFrameIndex===({automatic}) && elList.querySelector(".is-preview-anchor")?.dataset.id==desktopFrameIndex'
    frame_matches="""(()=>{
      const t=MOBILE_MQ.matches ? scrubSelectedTime : desktopFrameIndex===null ? '' : scrubTime(rowAt(desktopFrameIndex).t);
      if(!t || t==='—')return false;
      const seconds=t.split(':').reduce((n,v)=>60*n+Number(v),0);
      const frame=DATA.vod_preview.frames.reduce((a,b)=>Math.abs(a[0]-seconds)<=Math.abs(b[0]-seconds) ? a : b);
      return elVodPreviewImg.getAttribute('src')===frame[1];
    })()"""
    await load('new',1440)
    await mouse('mouseMoved',[10,10])
    await wait(selection_matches)
    await wait(frame_matches)
    points=await evaluate(f"""({visible}).slice(1,4).map(([id,r])=>[id,r.right-8,r.top+8])""")
    first,second,third=points
    await mouse('mouseMoved',first[1:])
    await wait(f'desktopFrameIndex==={first[0]} && !elList.querySelector(".is-preview-anchor")')
    await mouse('mousePressed',first[1:])
    await mouse('mouseReleased',first[1:])
    await wait(f'elList.querySelector(".is-chat-pinned")?.dataset.id=={first[0]}')
    await mouse('mouseMoved',second[1:])
    assert await evaluate(f'desktopFrameIndex==={first[0]} && elList.querySelector(".row:hover")?.dataset.id=={second[0]}')
    # Real username hover and click preserve the existing context panel.
    nick=await evaluate(f"""(()=>{{
      const r=elList.querySelector('.row[data-id="{third[0]}"] .user').getBoundingClientRect();
      return [r.left+r.width/2,r.top+r.height/2];
    }})()""")
    await mouse('mouseMoved',nick)
    assert await evaluate('elList.querySelector(".user:hover")!==null')
    await mouse('mousePressed',nick)
    await mouse('mouseReleased',nick)
    assert await evaluate(f'!chatContext.hidden && elList.querySelector(".is-chat-pinned")?.dataset.id=={first[0]}')
    await mouse('mouseMoved',[10,10])
    await mouse('mousePressed',[10,10])
    await mouse('mouseReleased',[10,10])
    await wait(selection_matches)
    await evaluate('elVodCard.click();true')
    await wait('desktopPreviewCollapsed && desktopFrameIndex===null && !elList.querySelector(".is-preview-anchor")')
    await mouse('mouseMoved',first[1:])
    await mouse('mousePressed',first[1:])
    await mouse('mouseReleased',first[1:])
    await wait('!!elList.querySelector(".is-chat-pinned") && desktopFrameIndex===null')
    await evaluate("window.dispatchEvent(new Event('blur'));true")
    await wait('!elList.querySelector(".is-chat-pinned")')
    await mouse('mouseMoved',[10,10])
    await evaluate('elDesktopPreviewToggle.click();true')
    await wait(selection_matches)
    await mouse('mouseMoved',first[1:])
    await mouse('mousePressed',first[1:])
    await mouse('mouseReleased',first[1:])
    await wait('!!elList.querySelector(".is-chat-pinned")')
    await evaluate("scrollTo(0,elList.offsetTop+heightBefore(Math.floor(blockCount()*.7)));true")
    await mouse('mouseMoved',[10,10])
    await wait('!windowBusy && desktopFrameIndex>rows.length/2 && !elList.querySelector(".is-chat-pinned")')
    await wait(frame_matches)
    assert await evaluate("elList.querySelectorAll('.row').length<=2400")
    point=await evaluate(f'({visible}).slice(1,2).map(([id,r])=>[r.right-8,r.top+8])[0]')
    await mouse('mousePressed',point)
    await mouse('mouseReleased',point)
    await wait('!!elList.querySelector(".is-chat-pinned")')
    await resize(1024)
    await wait('innerWidth===1024 && !elList.querySelector(".is-chat-pinned")')
    await mouse('mouseMoved',[10,10])
    await evaluate("elQ.value='unlikely-preview-search-miss';elQ.dispatchEvent(new Event('input'));true")
    await wait('!FILTERING && !windowBusy && rowCount()===0 && elVodPreviewImg.src===DATA.vod_preview.cover')
    await evaluate("elQ.value='';elU.value=decodeMessage(rows[rows.length-1]).user;elU.dispatchEvent(new Event('input'));true")
    await wait('!FILTERING && !windowBusy && rowCount()>0 && rowCount()<rows.length')
    await wait(selection_matches)

    # A single huge row spanning the viewport must still control the frame.
    await load('new',1440)
    await evaluate("elList.querySelector('.row').style.minHeight='1400px';scrollTo(0,100);true")
    await wait('desktopFrameIndex===0 && elList.querySelector(".is-preview-anchor")?.dataset.id==="0"')

    await load('new',390,720)
    await expand_mobile()
    await wait(f'{ready}===220 && scrubIndex===0')
    await evaluate('elVodCard.click();true')
    await wait('storyboardMode && !finishPreviewFlight')
    await evaluate("elScrubber.dispatchEvent(new KeyboardEvent('keydown',{key:'End'}));true")
    await wait('!scrubActive() && scrubIndex===rowCount()-1')
    await wait(frame_matches)
    await evaluate("scrollTo(0,300);true")
    await wait('!windowBusy && !scrubActive() && scrubIndex<100')
    await wait(frame_matches)
    assert await evaluate('!elList.querySelector(".is-chat-pinned,.is-preview-anchor")')
    header_height=await evaluate('elHeader.offsetHeight')
    await evaluate("""window.layoutCalls=0;window.scrollWrites=0;
      const layout=resizeStoryboardPanel;
      resizeStoryboardPanel=()=>{layoutCalls++;layout();};
      const go=scrollTo,by=scrollBy;
      window.scrollTo=(...a)=>{scrollWrites++;return go(...a);};
      window.scrollBy=(...a)=>{scrollWrites++;return by(...a);};
      const originalHeight=visualViewport.height;
      Object.defineProperty(visualViewport,'height',{configurable:true,get:()=>originalHeight-40});
      visualViewport.dispatchEvent(new Event('resize'));
      visualViewport.dispatchEvent(new Event('scroll'));
      true""")
    await evaluate('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>resolve(true))))')
    assert await evaluate('layoutCalls===0 && scrollWrites===0 && elHeader.offsetHeight')==header_height
    x,y=await evaluate('[innerWidth/2,innerHeight-60]')
    assert y-75>header_height
    before=await evaluate('scrollY')
    await call('Input.dispatchTouchEvent',dict(type='touchStart',touchPoints=[dict(x=x,y=y)]))
    for distance in (12,24,38,54,74,58,40,20,0):
        await call('Input.dispatchTouchEvent',dict(type='touchMove',touchPoints=[dict(x=x,y=y-distance)]))
        if distance==74:
            turned=await evaluate("delete visualViewport.height;visualViewport.dispatchEvent(new Event('resize'));scrollY")
    await call('Input.dispatchTouchEvent',dict(type='touchEnd',touchPoints=[]))
    assert turned>before
    await wait(f'scrollY<{turned}')
    assert await evaluate('layoutCalls===0 && elHeader.offsetHeight')==header_height
    await evaluate('elPanelHandle.click();true')
    await wait('!storyboardMode && !finishPreviewFlight')
    await evaluate('elPanelHandle.click();true')
    await wait('!PANEL_EXPANDED && !panelAnimating')

    comparison=[]
    for width in (390,1440):
        for version in ('base','new'):
            await load(version,width,720)
            if width==390:await expand_mobile()
            result=await evaluate("""(async()=>{
              const times=[];let inline=0,lookups=0;
              const select=nearestStoryFrame;
              nearestStoryFrame=(...args)=>{lookups++;return select(...args);};
              for(const part of [0,.1,.25,.4,.6,.8,.3,.05]){
                const before=lookups,start=performance.now();
                scrollTo(0,elList.offsetTop+heightBefore(Math.floor(blockCount()*part)));
                document.dispatchEvent(new Event('scroll'));
                inline+=lookups-before;
                await new Promise(requestAnimationFrame);
                times.push(performance.now()-start);
                await new Promise(resolve=>setTimeout(resolve,175));
              }
              return {times,inline,lookups};
            })()""")
            assert result['inline']==0
            comparison.append(dict(width=width,version=version,median_ms=round(median(result['times']),1),
                                   max_ms=round(max(result['times']),1),frame_lookups=result['lookups']))
    assert not errors,errors
    assert not any('/storyboards/' in url or '/cf_vods/test/' in url for url in requests),requests
    print(json.dumps(dict(navigation='PASS',rows=count,offline=True,comparison=comparison)),flush=True)
