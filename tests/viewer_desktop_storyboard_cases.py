"""Offline unified preview regression and focused scroll timing."""
import json
from statistics import median


async def check_unified_preview(call,evaluate,wait,out,args,errors,requests):
    count=args.sizes[0]
    await call('Network.enable')
    await call('Network.emulateNetworkConditions',dict(offline=True,latency=0,downloadThroughput=0,uploadThroughput=0))

    async def resize(width,height=900):
        await call('Emulation.setDeviceMetricsOverride',dict(
            width=width,height=height,deviceScaleFactor=1,mobile=width<600))

    async def load(version,width,height=900):
        await resize(width,height)
        await call('Page.navigate',dict(url=(out/f'{version}-{count}-offline.html').as_uri()))
        await wait(f'document.readyState==="complete" && innerWidth==={width} && !windowBusy && rowCount()>1')
        if width<=900:
            await evaluate('elPanelHandle.click();true')
            await wait('PANEL_EXPANDED && !panelAnimating')

    async def motion(mode):
        await call('Emulation.setEmulatedMedia',dict(features=[dict(name='prefers-reduced-motion',value=mode)]))

    ready='elVodPreviewImg.complete && elVodPreviewImg.naturalWidth'
    rects="""[elHeader,elList,elTitle,elVodCard,elScrubber].map(n=>{
      const r=n.getBoundingClientRect();return [r.x,r.y,r.width,r.height];
    })"""
    await motion('no-preference')
    for width in (320,390,901,1024,1440):
        for version,pixels in (('no-preview',0),('no-storyboard',1280),('new',220)):
            await load(version,width)
            await wait(f'{ready}==={pixels}')
            assert await evaluate("""elVodLink.textContent==='🔗 Ссылка' && !elVodLink.hidden
              && elVodLink.parentElement.lastElementChild===elVodLink
              && !document.querySelector('#desktopStoryboard,#storyboardToggle,.snapshot-overlay')
              && document.querySelectorAll('#vodPreviewImg').length===1""")
            assert await evaluate("""window.open=(...args)=>{window.opened=args;};
              elVodLink.click();opened.join('|')==='https://www.twitch.tv/videos/123|_blank|noopener,noreferrer'""")
            boxes=await evaluate(rects)
            assert await evaluate('document.documentElement.scrollWidth<=innerWidth'),(width,version,boxes)
            if width>900:
                assert boxes[0][2]==312 and boxes[1][2]==515
                if pixels:
                    assert boxes[2][1]+boxes[2][3]<=boxes[3][1] and boxes[3][2]>250
                    assert await evaluate('elVodCard.getBoundingClientRect().bottom<=elMobileChannel.getBoundingClientRect().top')
            else:
                assert boxes[4][2]>0 and boxes[3][2]==(132 if pixels else 0)
            if not pixels:
                assert await evaluate('elVodCard.hidden && !elVodPreviewImg.hasAttribute("src")')
                assert boxes[2][2]>=boxes[0][2]-50,(width,boxes)
                continue
            await evaluate("""window.originalImage=elVodPreviewImg;window.originalSource=elVodPreviewImg.src;
              elVodCard.click();true""")
            assert await evaluate("""previewOpened && !!finishPreviewFlight && elVodPreviewImg.src===originalSource
              && elVodPreviewImg.getAnimations()[0].effect.getTiming().duration===250
              && !document.querySelector('.snapshot-overlay') && document.body.style.overflow===''""")
            await wait('!finishPreviewFlight')
            assert await evaluate('elVodPreviewImg===originalImage')
            if width<=900:
                assert await evaluate("""storyboardMode && elPanelContent.hidden
                  && elVodPreviewImg.parentElement===elMobilePreviewMedia
                  && getComputedStyle(elPanelHandle.querySelector('.panel-exit')).display!=='none'""")
                assert (await evaluate(rects))[4]==boxes[4]
                await evaluate('elPanelHandle.click();true')
            else:
                assert await evaluate("""!elDesktopPreview.hidden && !elDesktopPreview.querySelector('#vodLink')
                  && elVodPreviewImg.getBoundingClientRect().width>elVodCard.clientWidth""")
                await evaluate('elPreviewClose.click();true')
            assert await evaluate('!previewOpened && !!finishPreviewFlight')
            await wait('!finishPreviewFlight && elVodPreviewImg.parentElement===elPreviewHome')
            assert await evaluate('document.activeElement===elVodCard && elVodPreviewImg.src===originalSource')

    await load('new',1440,320)
    assert await evaluate('elHeader.getBoundingClientRect().bottom<=innerHeight && elHeader.scrollHeight>elHeader.clientHeight')
    await evaluate('elVodLink.focus();true')
    assert await evaluate('document.activeElement===elVodLink && elVodLink.getBoundingClientRect().bottom<=innerHeight')
    await resize(1440)
    await wait('innerHeight===900')
    assert await evaluate('elTitle.scrollHeight<=Math.ceil(parseFloat(getComputedStyle(elTitle).lineHeight))')
    await evaluate("elTitle.textContent='Long title '.repeat(60);true")
    await wait('elTitle.getBoundingClientRect().height>100 && elTitle.scrollHeight<=elTitle.clientHeight')
    assert await evaluate('elTitle.getBoundingClientRect().bottom<=elVodCard.getBoundingClientRect().top')

    await motion('reduce')
    await load('new',901)
    await wait(f'{ready}===220')
    await evaluate('elVodCard.click();true')
    assert await evaluate('previewOpened && !finishPreviewFlight')
    await resize(900)
    await wait('MOBILE_MQ.matches && !previewOpened && !PANEL_EXPANDED && elDesktopPreview.hidden')
    await evaluate('elPanelHandle.click();true')
    await wait('PANEL_EXPANDED && !panelAnimating')
    await evaluate('elVodCard.click();elPanelHandle.click();elVodCard.click();true')
    assert await evaluate('previewOpened && storyboardMode && !finishPreviewFlight')
    await resize(901)
    await wait('!previewOpened && !storyboardMode && elVodPreviewImg.parentElement===elPreviewHome')
    await motion('no-preference')

    correct="""(()=>{
      const row=[...elList.querySelectorAll('.row')].find(n=>{
        const r=n.getBoundingClientRect();return r.bottom>viewportInset() && r.top<innerHeight;
      });
      if(!row)return false;
      const t=rowAt(+row.dataset.id).t.split(':').reduce((n,v)=>60*n+Number(v),0);
      const frame=DATA.vod_preview.frames.reduce((a,b)=>Math.abs(a[0]-t)<=Math.abs(b[0]-t) ? a : b);
      return elVodPreviewImg.getAttribute('src')===frame[1];
    })()"""
    for width in (390,1440):
        await load('new',width,720)
        await wait(correct)
        await evaluate("""window.writes=[];new MutationObserver(records=>{
          for(const record of records)writes.push(performance.now());
        }).observe(elVodPreviewImg,{attributeFilter:['src']});
        scrollBy(0,elList.querySelector('.row').getBoundingClientRect().top-viewportInset()+4);true""")
        await wait('visibleAnchor(0)?.id===0 && visibleAnchor(0).top<viewportInset()')
        await wait(correct)
        await evaluate('new Promise(resolve=>setTimeout(()=>resolve(true),180))')
        assert await evaluate('writes.length===0')
        await evaluate('scrollBy(0,300);true')
        await wait(correct)
        await evaluate("""for(const part of [.3,.45,.2,.68]){
          scrollTo(0,elList.offsetTop+heightBefore(Math.floor(blockCount()*part)));
        }true""")
        await wait('!windowBusy && visibleAnchor(0)?.id>rows.length/2')
        await wait(correct)
        assert await evaluate("elList.querySelectorAll('.row').length<=2400")
        await evaluate('scrollTo(0,0);true')
        await wait(correct)
        await evaluate("elQ.value='unlikely-preview-search-miss';elQ.dispatchEvent(new Event('input'));true")
        await wait('!FILTERING && !windowBusy && rowCount()===0 && elVodPreviewImg.src===DATA.vod_preview.cover')
        await evaluate("elQ.value='';elU.value=decodeMessage(rows[rows.length-1]).user;elU.dispatchEvent(new Event('input'));true")
        await wait('!FILTERING && !windowBusy && rowCount()>0 && rowCount()<rows.length')
        await wait(correct)
        await evaluate("elU.value='';elU.dispatchEvent(new Event('input'));true")
        await wait('!FILTERING && !windowBusy && rowCount()===rows.length')
        if width<=900:
            await evaluate('elVodCard.click();true')
            await wait('storyboardMode && !finishPreviewFlight')
            await evaluate("elScrubber.dispatchEvent(new KeyboardEvent('keydown',{key:'End'}));true")
            await wait('!scrubActive() && scrubIndex===rowCount()-1')
            await wait(correct)
        assert await evaluate('writes.slice(1).every((time,i)=>time-writes[i]>=110)'),await evaluate('writes')

    measurements=[]
    for width in (390,1440):
        for version in ('base','new','no-storyboard'):
            await load(version,width,720)
            sample=await evaluate("""(async()=>{
              const samples=[];let lookups=0,inline=0;
              const select=nearestStoryFrame;
              nearestStoryFrame=(...args)=>{lookups++;return select(...args);};
              for(const part of [0,.1,.25,.4,.6,.8,.3,.05]){
                const before=lookups,start=performance.now();
                scrollTo(0,elList.offsetTop+heightBefore(Math.floor(blockCount()*part)));
                document.dispatchEvent(new Event('scroll'));
                inline+=lookups-before;
                await new Promise(requestAnimationFrame);
                samples.push(performance.now()-start);
                await new Promise(resolve=>setTimeout(resolve,175));
              }
              return {samples,lookups,inline};
            })()""")
            assert sample['inline']==0
            if version=='no-storyboard':
                assert sample['lookups']==0
            else:
                measurements.append(dict(width=width,version=version,median_ms=round(median(sample['samples']),1),
                    max_ms=round(max(sample['samples']),1),lookups=sample['lookups'],inline_lookups=sample['inline']))
    assert not errors,errors
    assert not any('/storyboards/' in url or '/cf_vods/test/' in url for url in requests),requests
    print(json.dumps(dict(unified_preview='PASS',rows=count,offline=True,comparison=measurements)),flush=True)
