"""Offline Chromium coverage for message navigation and the preview panels."""
import base64
import io
import json
from statistics import median

from PIL import Image


async def check_unified_preview(call,evaluate,wait,out,args,errors,requests):
    count=args.sizes[0]
    await call('Network.enable')
    await call('Network.emulateNetworkConditions',dict(offline=True,latency=0,downloadThroughput=0,uploadThroughput=0))

    async def resize(width,height=900,scale=1):
        await call('Emulation.setDeviceMetricsOverride',dict(
            width=width,height=height,deviceScaleFactor=scale,mobile=width<600))
        await call('Emulation.setTouchEmulationEnabled',dict(enabled=width<600,maxTouchPoints=1))

    async def load(version,width,height=900,mode='offline'):
        await resize(width,height)
        await call('Page.navigate',dict(url=(out/f'{version}-{count}-{mode}.html').as_uri()))
        await wait(f'document.readyState==="complete" && innerWidth==={width} && !windowBusy && rowCount()>1')
        if version not in ('base','no-preview') and width>=901:
            await wait('desktopPreviewCollapsed && !elDesktopPreviewToggle.hidden && !elList.querySelector(".is-preview-anchor")')
            await evaluate('elDesktopPreviewToggle.click();true')
            await wait('!elDesktopPreview.getAnimations().length')

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
    for width in (320,390):
        await load('new',width,720)
        for expanded in (False,True):
            if expanded:
                await expand_mobile()
            await evaluate("requestScrub(rowCount()-1,'end');true")
            await wait('!scrubActive() && atChatEnd() && scrubIndex===rowCount()-1')
            await evaluate("""(()=>{window.endSamples=[];
              const record=()=>{
                const end=document.scrollingElement.scrollHeight-document.scrollingElement.clientHeight;
                endSamples.push({y:scrollY,end,padding:elList.style.paddingTop,header:viewportInset(),
                  hit:document.elementFromPoint(10,1)?.closest('header')===elHeader});
                if(panelAnimating) requestAnimationFrame(record);
              };
              elPanelHandle.click();requestAnimationFrame(record);return true;})()""")
            await wait('!panelAnimating && atChatEnd() && endSamples.length>1')
            samples=await evaluate('endSamples')
            assert all(s['hit'] and abs(s['end']-s['y'])<=1 for s in samples),samples
            assert len({s['y'] for s in samples[:-1]})==1,samples
            assert len({s['padding'] for s in samples[:-1]})==1,samples
            assert await evaluate("""(()=>{
              const last=elList.querySelector('.row[data-id="'+(rowCount()-1)+'"]').getBoundingClientRect();
              const css=getComputedStyle(elList);
              return last.top>=viewportInset() && Math.abs(last.bottom-elList.getBoundingClientRect().bottom)<1
                && last.bottom<=innerHeight+1 && css.outlineStyle==='none' && css.boxShadow.includes('inset')
                && parseFloat(css.paddingTop)-viewportInset()<=1.5 && css.paddingBottom==='0px';
            })()""")
            if not expanded:
                await evaluate('elPanelHandle.click();true')
                await wait('!PANEL_EXPANDED && !panelAnimating && atChatEnd()')
        before=await evaluate('[scrollY,scrubIndex]')
        await evaluate("""(()=>{
          const height=visualViewport.height;
          Object.defineProperty(visualViewport,'height',{configurable:true,get:()=>height-32});
          visualViewport.dispatchEvent(new Event('resize'));return true;
        })()""")
        await wait('!messageSelectionFrame')
        assert await evaluate('[scrollY,scrubIndex]')==before
        await evaluate("delete visualViewport.height;visualViewport.dispatchEvent(new Event('resize'));true")
        x,y=await evaluate('[innerWidth/2,innerHeight-100]')
        await call('Input.dispatchTouchEvent',dict(type='touchStart',touchPoints=[dict(x=x,y=y-120)]))
        for dy in (20,40,70,100,120):
            await call('Input.dispatchTouchEvent',dict(type='touchMove',touchPoints=[dict(x=x,y=y-120+dy)]))
        await call('Input.dispatchTouchEvent',dict(type='touchEnd',touchPoints=[]))
        await wait('!atChatEnd() && !panelAnimating')

    for width,mode in ((390,'offline'),(1440,'online')):
        await load('new',width,mode=mode)
        if width<901:
            await expand_mobile()
            assert await evaluate('scrubStart && scrubSelectedTime==="—" && elScrubber.getAttribute("aria-valuetext").startsWith("00:00:00 / ")')
            await evaluate("requestScrub(rowCount()-1,'end');true")
            await wait('!scrubActive() && atChatEnd() && scrubIndex===rowCount()-1')
            y=await evaluate('scrollY')
            await evaluate('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>resolve(true))))')
            assert await evaluate('scrollY')==y
            await evaluate("requestScrub(0,'start');true")
            await wait('!scrubActive() && scrollY===0 && scrubStart')
        assert await evaluate("""getComputedStyle(elList).paddingBottom==='0px'
          && getComputedStyle(elQ).borderColor===getComputedStyle(elExportBtn).borderColor""")
        if width>=901:
            await evaluate('scrollTo(0,400);true')
            await wait('desktopFrameIndex!==null')
        else:
            await evaluate('requestScrub(3);true')
            await wait('!scrubActive() && scrubIndex===3')
        await evaluate("""window.offlineTestDuration=meta.duration_seconds;
          const time=MOBILE_MQ.matches?scrubSelectedTime:scrubTime(rowAt(desktopFrameIndex).t);
          meta.duration_seconds=previewSeconds(time);
          if(MOBILE_MQ.matches)setScrubIndex(scrubIndex);
          refreshPreviewFrame();true""")
        assert await evaluate('!elPreviewHome.classList.contains("is-offline")')
        await evaluate("""meta.duration_seconds=Math.max(1,meta.duration_seconds-1);
          if(MOBILE_MQ.matches)setScrubIndex(scrubIndex);
          refreshPreviewFrame();true""")
        # Near the start synthetic timestamps can be identical; use a later row.
        if width<901:
            await evaluate('requestScrub(1000);true')
        else:
            await evaluate('scrollTo(0,300000);true')
        await wait('elPreviewHome.classList.contains("is-offline") && !elVodPreviewImg.hasAttribute("src")')
        if width<901:
            assert await evaluate("""elScrubber.getAttribute('aria-valuetext')==='OFFLINE'
              && getComputedStyle(elScrubber.querySelector('.scrub-times'),'::before').color
                ===getComputedStyle(elScrubber.querySelector('.scrub-times')).color""")
        assert await evaluate('elList.querySelectorAll(".row").length<=2400')

    for width in (390,1440):
        await load('no-preview',width)
        if width<901:
            await expand_mobile()
            await evaluate("meta.duration_seconds=1;requestScrub(1000);true")
        else:
            await evaluate('meta.duration_seconds=1;scrollTo(0,300000);true')
            await wait('!elDesktopPreview.hidden && !elDesktopPreviewToggle.hidden')
            await evaluate('elDesktopPreviewToggle.click();true')
        await wait('!elVodCard.hidden && elPreviewHome.classList.contains("is-offline") && !elVodPreviewImg.hasAttribute("src")')

    for width in (390,901,1024,1440):
        for version,pixels in (('no-preview',0),('no-storyboard',1280),('new',220)):
            await load(version,width)
            if width<901:
                await expand_mobile()
            await wait(f'{ready}==={1280 if version=="new" else pixels}')
            assert await evaluate("""elVodLink.textContent==='🔗 Ссылка' && !elVodLink.hidden
              && document.documentElement.scrollWidth<=innerWidth
              && document.querySelectorAll('#vodPreviewImg').length===1""")
            if width>=901:
                assert await evaluate("""(()=>{
                  const h=elHeader.getBoundingClientRect(),l=elList.getBoundingClientRect();
                  return h.width===336 && Math.abs(h.top-24)<1
                    && Math.abs(h.left-(document.documentElement.clientWidth-l.right))<2
                    && h.right<l.left;
                })()""")
                assert await evaluate(f'elDesktopPreview.hidden==={str(not pixels).lower()}')
                if pixels:
                    assert await evaluate("""elVodCard.parentElement===elDesktopPreview &&
                      Math.abs(elDesktopPreview.getBoundingClientRect().bottom-elPanelDetails.getBoundingClientRect().top)<1 &&
                      Math.abs(elDesktopPreview.getBoundingClientRect().top-25)<1 &&
                      getComputedStyle(elDesktopPreview).borderBottomWidth==='1px' &&
                      elPanelDetails.scrollHeight===elPanelDetails.clientHeight &&
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
                assert await evaluate('elPanelHandle.querySelector(".panel-exit path").getAttribute("d")==="m6 15 6-6 6 6"')
                assert await evaluate('elVodPreviewImg.parentElement===elMobilePreviewMedia && elScrubber.clientWidth>0')
                await evaluate('elPanelHandle.click();true')
                await wait('!storyboardMode && !finishPreviewFlight')
            else:
                assert await evaluate('elVodCard.hidden && elTitle.clientWidth>=elHeader.clientWidth-50')

    await load('new',1024,190)
    assert await evaluate('elPanelDetails.clientHeight>=46 && elDesktopPreview.getBoundingClientRect().top>=24')

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
    automatic=f"""(()=>{{
      const all={visible}, full=all.filter(([,r])=>r.top>=Math.max(0,viewportInset())-.5 && r.bottom<=innerHeight+.5);
      return full[0]?.[0]===0 ? null : (full[0] || all[0])?.[0] ?? null;
    }})()"""
    selection_matches=f'desktopFrameIndex===({automatic}) && (elList.querySelector(".is-preview-anchor")?.dataset.id ?? null)==desktopFrameIndex'
    frame_matches="""(()=>{
      const index=MOBILE_MQ.matches ? scrubIndex : desktopFrameIndex;
      if(index===null || (MOBILE_MQ.matches && scrubSelectedTime==='—')) return elVodPreviewImg.getAttribute('src')===(DATA.vod_preview.cover || null);
      const t=scrubTime(rowAt(index).t);
      if(!t || t==='—')return false;
      const seconds=t.split(':').reduce((n,v)=>60*n+Number(v),0);
      const frame=DATA.vod_preview.frames.filter(f=>f[0]<=seconds).at(-1);
      return elVodPreviewImg.getAttribute('src')===(frame?.[1] || DATA.vod_preview.cover || null);
    })()"""
    async def message_point(index,nickname=False):
        selector=f'.row[data-id="{index}"]'+(' .user' if nickname else '')
        x='r.left+r.width/2' if nickname else 'r.right-6'
        return await evaluate(f"""(()=>{{
          const r=elList.querySelector({json.dumps(selector)}).getBoundingClientRect();
          return [{x},r.top+r.height/2];
        }})()""")

    async def tap(point):
        await call('Input.dispatchTouchEvent',dict(type='touchStart',touchPoints=[dict(x=point[0],y=point[1])]))
        await call('Input.dispatchTouchEvent',dict(type='touchEnd',touchPoints=[]))

    async def touch_selection(width):
        await load('new',width,820)
        await call('Emulation.setTouchEmulationEnabled',dict(enabled=True,maxTouchPoints=1))
        if width<=900:
            await expand_mobile()
        index=await evaluate(f'({visible})[2][0]')
        point=await message_point(index)
        selected='scrubIndex' if width<=900 else 'desktopFrameIndex'
        await evaluate('window.pinCount=0;const originalPin=pinMessage;pinMessage=(...args)=>{pinCount++;originalPin(...args);};true')
        await tap(point)
        await wait(f'{selected}==={index} && elList.querySelector(".is-message-pinned")?.dataset.id=={index}')
        await wait(frame_matches)
        await evaluate("visualViewport.dispatchEvent(new Event('resize'));visualViewport.dispatchEvent(new Event('scroll'));true")
        assert await evaluate("pinCount===1 && !!elList.querySelector('.is-message-pinned')")
        await evaluate('scrollBy(0,30);true')
        await wait('!elList.querySelector(".is-message-pinned")')
        index=await evaluate(f'({visible})[2][0]')
        await tap(await message_point(index,True))
        try:
            await wait(f'!chatContext.hidden && {selected}==={index} && !!elList.querySelector(".user.is-nickname-highlighted")')
        except AssertionError as failure:
            state=await evaluate(f"""({{hidden:chatContext.hidden, selected:{selected}, pins:pinCount,
              anchor:contextAnchor?.outerHTML, pinned:elList.querySelector('.is-message-pinned')?.dataset.id,
              selection:getSelection()?.toString(), scrollY}})""")
            raise AssertionError((width,index,state,errors)) from failure
        await evaluate("visualViewport.dispatchEvent(new Event('resize'));true")
        assert await evaluate('!chatContext.hidden && pinCount===2')
        await call('Input.dispatchKeyEvent',dict(type='keyDown',key='Escape'))
        await wait('chatContext.hidden && !elList.querySelector(".is-message-pinned,.is-nickname-highlighted")')
        await tap(await message_point(index))
        await wait('!!elList.querySelector(".is-message-pinned")')
        point=await evaluate("[elHeader.getBoundingClientRect().left+5,elHeader.getBoundingClientRect().top+5]")
        await tap(point)
        await wait('!elList.querySelector(".is-message-pinned")')
        x,y=await message_point(index)
        await call('Input.dispatchTouchEvent',dict(type='touchStart',touchPoints=[dict(x=x,y=y)]))
        await call('Input.dispatchTouchEvent',dict(type='touchMove',touchPoints=[dict(x=x,y=y-70)]))
        await call('Input.dispatchTouchEvent',dict(type='touchEnd',touchPoints=[]))
        assert await evaluate('pinCount===3 && !elList.querySelector(".is-message-pinned")')

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
    await wait(f'desktopFrameIndex==={first[0]}')
    await evaluate("visualViewport.dispatchEvent(new Event('scroll'));true")
    await wait(f'elList.querySelector(".is-message-pinned")?.dataset.id==="{first[0]}"')
    await evaluate('scrollBy(0,15);true')
    await wait('!elList.querySelector(".is-message-pinned")')
    await evaluate('scrollTo(0,0);true')
    await wait('scrollY===0 && !windowBusy')
    await mouse('mousePressed',first[1:])
    await mouse('mouseReleased',first[1:])
    await wait(f'elList.querySelector(".is-message-pinned")?.dataset.id==="{first[0]}"')
    await mouse('mouseMoved',second[1:])
    await wait(f'''desktopFrameIndex==={first[0]} && elList.querySelector(".is-message-hovered")?.dataset.id=={second[0]}
      && !elList.querySelector(".is-preview-anchor")
      && ['.is-message-pinned','.is-message-hovered'].every(selector=>
        getComputedStyle(elList.querySelector(selector)).backgroundColor==='rgb(56, 56, 61)')''')
    # Real username hover and click preserve the existing context panel.
    nick=await evaluate(f"""(()=>{{
      const r=elList.querySelector('.row[data-id="{third[0]}"] .user').getBoundingClientRect();
      return [r.left+r.width/2,r.top+r.height/2];
    }})()""")
    await mouse('mouseMoved',nick)
    await wait('elList.querySelector(".user.has-ink-highlight")!==null')
    await mouse('mousePressed',nick)
    await mouse('mouseReleased',nick)
    await wait(f'!chatContext.hidden && desktopFrameIndex==={third[0]}')
    await call('Input.dispatchKeyEvent',dict(type='keyDown',key='Escape'))
    await mouse('mouseMoved',[10,10])
    await wait(f'chatContext.hidden && desktopFrameIndex==={third[0]} && !elList.querySelector(".is-nickname-highlighted")')
    await mouse('mouseMoved',[10,10])
    await mouse('mousePressed',[10,10])
    await mouse('mouseReleased',[10,10])
    await wait(selection_matches)
    x,y=first[1:]
    await call('Input.dispatchMouseEvent',dict(type='mousePressed',button='left',buttons=1,clickCount=1,x=x,y=y))
    await call('Input.dispatchMouseEvent',dict(type='mouseMoved',buttons=1,x=x-120,y=y))
    await call('Input.dispatchMouseEvent',dict(type='mouseReleased',button='left',buttons=0,clickCount=1,x=x-120,y=y))
    await mouse('mouseMoved',[10,10])
    await wait(selection_matches)
    await evaluate('getSelection().removeAllRanges();true')
    await evaluate('elVodCard.click();true')
    await wait('desktopPreviewCollapsed && !elList.querySelector(".is-preview-anchor")')
    await mouse('mouseMoved',first[1:])
    await mouse('mousePressed',first[1:])
    await mouse('mouseReleased',first[1:])
    await wait('!!elList.querySelector(".is-message-pinned")')
    await mouse('mouseMoved',[10,10])
    await evaluate('elDesktopPreviewToggle.click();true')
    await wait(f'desktopFrameIndex==={first[0]} && !elList.querySelector(".is-preview-anchor")')
    await evaluate("window.dispatchEvent(new Event('blur'));true")
    await wait(selection_matches)
    await mouse('mouseMoved',first[1:])
    await mouse('mousePressed',first[1:])
    await mouse('mouseReleased',first[1:])
    await wait(f'desktopFrameIndex==={first[0]}')
    await evaluate("scrollTo(0,elList.offsetTop+heightBefore(Math.floor(blockCount()*.7)));true")
    await mouse('mouseMoved',[10,10])
    await wait('!windowBusy && desktopFrameIndex>rows.length/2 && !elList.querySelector(".is-message-pinned")')
    await wait(frame_matches)
    assert await evaluate("elList.querySelectorAll('.row').length<=2400")
    pin_id,*point=await evaluate(f'({visible}).slice(1,2).map(([id,r])=>[id,r.right-8,r.top+8])[0]')
    await mouse('mousePressed',point)
    await mouse('mouseReleased',point)
    await wait(f'desktopFrameIndex==={pin_id}')
    await resize(1024)
    await wait('innerWidth===1024')
    await mouse('mouseMoved',[10,10])
    await wait('!resizeAnchor && !elList.querySelector(".is-message-pinned")')
    await wait(selection_matches)
    await evaluate("elQ.value='unlikely-preview-search-miss';elQ.dispatchEvent(new Event('input'));true")
    await wait('!FILTERING && !windowBusy && rowCount()===0 && elVodPreviewImg.src===DATA.vod_preview.cover && !elList.querySelector(".is-message-pinned")')
    await evaluate("elQ.value='';elU.value=decodeMessage(rows[rows.length-1]).user;elU.dispatchEvent(new Event('input'));true")
    await wait('!FILTERING && !windowBusy && rowCount()>0 && rowCount()<rows.length')
    await wait(selection_matches)

    for width in (1440,390):
        await touch_selection(width)

    await load('new',900)
    await expand_mobile()
    await mouse('mouseMoved',[5,5])
    assert await evaluate("""getComputedStyle(document.body).backgroundColor==='rgb(14, 14, 16)'
      && getComputedStyle(elHeader).backgroundColor==='rgb(24, 24, 27)'
      && getComputedStyle(elList).backgroundImage.includes('rgb(24, 24, 27)')""")
    narrow=await evaluate(f'({visible})[2][0]')
    nickname=await message_point(narrow,True)
    await mouse('mouseMoved',nickname)
    await wait(f'scrubIndex==={narrow} && !!elList.querySelector(".user.has-ink-highlight")')
    await mouse('mousePressed',nickname)
    await mouse('mouseReleased',nickname)
    await wait('!chatContext.hidden && !!elList.querySelector(".is-message-pinned")')
    await mouse('mouseMoved',[5,5])
    await wait(f'scrubIndex==={narrow} && !elList.querySelector(".is-nickname-highlighted")')
    await wait(frame_matches)
    assert await evaluate("""elScrubber.getAttribute('aria-valuenow')==Math.round(scrubPercent(scrubIndex,rowCount()))
      && elScrubber.querySelector('.scrub-times').textContent.startsWith(scrubTime(rowAt(scrubIndex).t)+' / ')
      && !elList.querySelector('.is-preview-anchor')""")
    await resize(901)
    await wait('!resizeAnchor && !elList.querySelector(".is-message-pinned")')

    await load('new',1440)
    await mouse('mouseMoved',[10,10])
    for event in ('zoom','visibility'):
        index=await evaluate(f'({visible})[2][0]')
        point=await message_point(index)
        await mouse('mousePressed',point)
        await mouse('mouseReleased',point)
        await wait('!!elList.querySelector(".is-message-pinned")')
        if event=='zoom':
            await call('Emulation.setPageScaleFactor',dict(pageScaleFactor=1.25))
            await wait('visualViewport.scale>1 && !elList.querySelector(".is-message-pinned")')
            await call('Emulation.setPageScaleFactor',dict(pageScaleFactor=1))
        else:
            await evaluate("""Object.defineProperty(document,'hidden',{configurable:true,value:true});
              document.dispatchEvent(new Event('visibilitychange'));true""")
            await wait('!elList.querySelector(".is-message-pinned")')
            await evaluate("delete document.hidden;document.dispatchEvent(new Event('visibilitychange'));true")

    mobile_automatic="""([...elList.querySelectorAll('.row')]
        .filter(row=>row.getBoundingClientRect().bottom<=elHeader.getBoundingClientRect().bottom)
        .at(-1)?.dataset.id ?? 0)*1"""
    await load('new',390)
    await wait("scrubSelectedTime==='—'")
    await evaluate("""window.touchSamples=[];
      const recordTouchScroll=()=>touchSamples.push({y:scrollY,index:scrubIndex,time:scrubSelectedTime,
        expected:[...elList.querySelectorAll('.row')].findLast(row=>row.getBoundingClientRect().bottom<=viewportInset())?.dataset.id ?? null});
      window.addEventListener('scroll',()=>requestAnimationFrame(recordTouchScroll),{passive:true});true""")
    await call('Input.dispatchTouchEvent',dict(type='touchStart',touchPoints=[dict(x=190,y=810)]))
    for y in range(800,79,-8):
        await call('Input.dispatchTouchEvent',dict(type='touchMove',touchPoints=[dict(x=190,y=y)]))
    await call('Input.dispatchTouchEvent',dict(type='touchEnd',touchPoints=[]))
    await wait('scrollY>150 && touchSamples.length>5 && !messageSelectionFrame')
    samples=await evaluate('touchSamples')
    assert any(sample['expected'] is not None for sample in samples), samples[-5:]
    assert all(a['index']<=b['index'] for a,b in zip(samples,samples[1:]) if a['y']<=b['y']),samples
    # Use a fresh document to isolate a viewport-only change from native fling.
    await load('new',390)
    await evaluate('scrollTo({top:240,behavior:"instant"});true')
    await wait(f'scrollY===240 && !messageSelectionFrame && !windowBusy && scrubIndex===({mobile_automatic})')
    before=await evaluate('[scrollY,scrubIndex,scrubSelectedTime]')
    await evaluate("""const vh=visualViewport.height;
      Object.defineProperty(visualViewport,'height',{configurable:true,get:()=>vh-48});
      visualViewport.dispatchEvent(new Event('resize'));true""")
    await wait('!messageSelectionFrame')
    after=await evaluate('[scrollY,scrubIndex,scrubSelectedTime]')
    assert after==before,(before,after)
    await evaluate("delete visualViewport.height;visualViewport.dispatchEvent(new Event('resize'));true")
    await evaluate('scrollTo(0,0);true')
    await wait("scrollY===0 && scrubSelectedTime==='—'")

    for width,version in ((1440,'offline'),(390,'offline'),(390,'online')):
        await load('new',width,mode=version)
        mobile=width<901
        if mobile:
            await expand_mobile()
            await wait("scrubSelectedTime==='—' && elVodPreviewImg.src===embeddedCover")
        await mouse('mouseMoved',[10,10])
        selection=f'scrubIndex===({mobile_automatic})' if mobile else selection_matches
        for filter_user in (False,True):
            if filter_user:
                await evaluate("elU.value='user1';applyFilters()")
                await wait('!windowBusy && !FILTERING && rowCount()>1 && rowCount()<rows.length')
            await evaluate('scrollTo(0,document.scrollingElement.scrollHeight);true')
            await wait(f'atChatEnd() && !windowBusy && !windowFrame && !messageSelectionFrame && ({selection})')
            await wait(f'({selection}) && ({frame_matches}) && elVodPreviewImg.getAttribute("src")!==embeddedCover')
            state=await evaluate("""({hasCover:elVodPreviewImg.src===embeddedCover, rows:elList.querySelectorAll('.row').length,
              desktopFrameIndex,scrubIndex,scrubSelectedTime,windowStart,windowEnd})""")
            assert not state['hasCover'] and state['rows']<=2400,(width,version,filter_user,state)
            await evaluate('scrollBy(0,-130);true')
            await wait(f'!atChatEnd() && !windowBusy && ({selection})')
            await wait(frame_matches)

    await load('no-cover',390)
    await expand_mobile()
    assert await evaluate('scrubIndex===0 && !elVodPreviewImg.hasAttribute("src")')
    await evaluate("requestScrub(Math.floor(rowCount()/2));true")
    await wait('!scrubActive() && elVodPreviewImg.hasAttribute("src")')
    await evaluate('requestScrub(rowCount()-1);true')
    await wait(f'!scrubActive() && ({frame_matches}) && elVodPreviewImg.hasAttribute("src")')

    await load('new',1440)
    assert await evaluate("""getComputedStyle(document.body).backgroundColor==='rgb(14, 14, 16)'
      && getComputedStyle(elList).backgroundColor==='rgb(24, 24, 27)'
      && !elList.querySelector('.is-preview-anchor')""")
    await evaluate('scrollTo(0,60);true')
    await wait(selection_matches)
    palette=await evaluate("""[document.body,elList,elQ,elExportBtn,elList.querySelector('.is-preview-anchor')].map(n=>{
      const s=getComputedStyle(n);return [s.color,s.backgroundColor,s.borderColor];
    })""")
    def luminance(color):
        values=[int(value.strip())/255 for value in color[4:-1].split(',')]
        return sum(weight*(value/12.92 if value<=.04045 else ((value+.055)/1.055)**2.4)
                   for weight,value in zip((.2126,.7152,.0722),values))
    def contrast(first,second):
        dark,light=sorted((luminance(first),luminance(second)))
        return (light+.05)/(dark+.05)
    assert all(contrast(text,background)>=4.5 for text,background,_ in palette)
    assert palette[2][2]==palette[3][2]=='rgb(73, 73, 80)'
    await evaluate('scrollTo(0,0);true')
    await wait('scrollY===0 && !windowBusy')
    ink_gaps=[]
    for font,scale in (('Arial',1),('Georgia',1.25)):
        await resize(1440,900,scale)
        await wait(f'devicePixelRatio==={scale}')
        heights=[]
        for word in ('xxx','HHH','gyp'):
            await mouse('mouseMoved',[10,10])
            box=await evaluate(f"""(()=>{{
              const n=elList.querySelector('.row[data-id="1"] .user');
              n.textContent='{word}';n.style.fontFamily='{font}';n.style.color='#ffffff';
              const r=n.getBoundingClientRect();
              return [r.left,r.top,r.width,r.height,n.closest('.row').offsetHeight];
            }})()""")
            await mouse('mouseMoved',[box[0]+box[2]/2,box[1]+box[3]/2])
            await wait('!!elList.querySelector(".user.has-ink-highlight:hover")')
            top,height=await evaluate("""(()=>{
              const n=elList.querySelector('.user:hover'),p=getComputedStyle(n,'::before');
              return [parseFloat(p.top),parseFloat(p.height)];
            })()""")
            assert await evaluate('elList.querySelector(".user:hover").closest(".row").offsetHeight')==box[4]
            heights.append(height)
            shot=await call('Page.captureScreenshot',dict(format='png',clip=dict(
                x=int(box[0])-3,y=int(box[1]+top)-3,width=int(box[2])+9,height=int(height)+8,scale=1)))
            with Image.open(io.BytesIO(base64.b64decode(shot['data']))) as image:
                rgb=image.convert('RGB')
                # Read the centre strip so rounded corners cannot bias the bounds.
                band=range(3,rgb.width-3)
                background=[y for y in range(rgb.height) if any(rgb.getpixel((x,y))==(83,83,95) for x in band)]
                glyph=[y for y in range(rgb.height) if any(min(rgb.getpixel((x,y)))>=205 for x in band)]
            assert background and glyph,(font,word,top,height)
            gaps=(min(glyph)-min(background),max(background)-max(glyph))
            assert min(gaps)>=0 and abs(gaps[0]-gaps[1])<=3*scale,(font,word,gaps)
            ink_gaps.append(dict(font=font,scale=scale,word=word,gaps=gaps))
        assert heights[1]>heights[0],(font,heights)

    # A single huge row spanning the viewport must still control the frame.
    await load('new',1440)
    await evaluate("elList.querySelector('.row').style.minHeight='1400px';scrollTo(0,100);true")
    await wait('desktopFrameIndex===0 && elList.querySelector(".is-preview-anchor")?.dataset.id==="0"')
    await wait(frame_matches)

    await load('new',390,720)
    await expand_mobile()
    await wait(f'{ready}===1280 && scrubIndex===0')
    await evaluate('elVodCard.click();true')
    await wait('storyboardMode && !finishPreviewFlight')
    await evaluate("elScrubber.dispatchEvent(new KeyboardEvent('keydown',{key:'End'}));true")
    await wait('!scrubActive() && scrubIndex===rowCount()-1')
    await wait(frame_matches)
    await evaluate("scrollTo(0,300);true")
    await wait('!windowBusy && !scrubActive() && scrubIndex<100')
    await wait(frame_matches)
    assert await evaluate('!elList.querySelector(".is-message-pinned,.is-preview-anchor")')
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
              const select=typeof previewStoryFrame==='function' ? previewStoryFrame : nearestStoryFrame;
              const instrument=(...args)=>{lookups++;return select(...args);};
              if(typeof previewStoryFrame==='function') previewStoryFrame=instrument;
              else nearestStoryFrame=instrument;
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
    print(json.dumps(dict(navigation='PASS',rows=count,offline=True,ink_gaps=ink_gaps,comparison=comparison)),flush=True)
