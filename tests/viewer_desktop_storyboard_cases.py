"""Focused offline checks and scroll comparison for desktop storyboard."""
import json
from statistics import median


async def check_desktop_storyboard(call, evaluate, wait, out, args, errors, requests):
    count = args.sizes[0]
    await call('Network.enable')
    await call('Network.emulateNetworkConditions', dict(
        offline=True, latency=0, downloadThroughput=0, uploadThroughput=0))

    async def resize(width, height=900, mobile=False):
        await call('Emulation.setDeviceMetricsOverride', dict(
            width=width, height=height, deviceScaleFactor=1, mobile=mobile))

    async def load(version, width, height=900, mobile=False):
        await resize(width,height,mobile)
        await call('Page.navigate',dict(url=(out/f'{version}-{count}-offline.html').as_uri()))
        await wait(f'document.readyState==="complete" && innerWidth==={width} && !windowBusy && rowCount()>1')

    geometry = """[elHeader,elList,elVodCard,elExportBtn,elToggleTime].map(node=>{
      const r=node.getBoundingClientRect();return [r.x,r.y,r.width,r.height];
    })"""
    image = 'document.querySelector("#desktopStoryboard img")'
    src = f'{image}.getAttribute("src")'
    first_frame = f'{src}===DATA.vod_preview.frames[0][1] && {image}.naturalWidth===220'
    for width in (901,1024,1440):
        await load('base',width)
        original=await evaluate(geometry)
        await load('no-storyboard',width)
        assert await evaluate(geometry)==original
        assert await evaluate(f'{image}.parentElement.hidden && !({src})')
        await load('new',width)
        await wait(first_frame)
        updated=await evaluate(geometry)
        assert updated[0][:3]==original[0][:3] and updated[0][3]>original[0][3]
        assert updated[1:]==original[1:],(width,updated,original)
        assert await evaluate(f"""(()=>{{
          const frame={image}.getBoundingClientRect(), button=elExportBtn.getBoundingClientRect();
          return frame.top>=button.bottom && Math.abs(frame.width/frame.height-220/124)<.04
            && Math.abs(frame.left+frame.width/2-(elHeader.getBoundingClientRect().left+elHeader.offsetWidth/2))<2;
        }})()""")
    await resize(1440,300)
    await wait('innerHeight===300')
    assert await evaluate("""elHeader.getBoundingClientRect().bottom<=innerHeight
      && elHeader.scrollHeight>elHeader.clientHeight && elList.clientWidth===515""")
    await evaluate('elToggleTime.focus();true')
    assert await evaluate('document.activeElement===elToggleTime && elToggleTime.getBoundingClientRect().bottom<=innerHeight')
    await resize(1440)

    # Geometry oracle uses the actual visible DOM row; frame oracle uses linear
    # distance and no viewer selection helper.
    correct = f"""(()=>{{
      const row=[...elList.querySelectorAll('.row')].find(row=>{{
        const r=row.getBoundingClientRect();return r.bottom>0 && r.top<innerHeight;
      }});
      if(!row)return false;
      const seconds=rowAt(+row.dataset.id).t.split(':').reduce((n,t)=>60*n+Number(t),0);
      const closest=DATA.vod_preview.frames.reduce((a,b)=>Math.abs(a[0]-seconds)<=Math.abs(b[0]-seconds) ? a : b);
      return {src}===closest[1];
    }})()"""
    await evaluate("""window.writes=[];new MutationObserver(records=>{
      for(const record of records)writes.push(performance.now());
    }).observe(document.querySelector('#desktopStoryboard img'),{attributeFilter:['src']});true""")
    await evaluate("""scrollTo(0,elList.querySelector('.row').getBoundingClientRect().top+scrollY+4);true""")
    await wait('visibleAnchor(0)?.id===0 && visibleAnchor(0)?.top<0')
    await wait(correct)
    await evaluate('new Promise(resolve=>setTimeout(resolve,250))')
    assert await evaluate('writes.length===0')
    await evaluate('scrollBy(0,300);true')
    await wait(correct)
    await evaluate("""for(const n of [19,7,18,41]){
      scrollTo(0,elList.offsetTop+heightBefore(n));document.dispatchEvent(new Event('scroll'));
    }true""")
    await wait('!windowBusy && visibleAnchor(0)?.id>10000')
    await wait(correct)
    assert await evaluate(f'{src}!==DATA.vod_preview.frames[0][1]')
    assert await evaluate("elList.querySelectorAll('.row').length<=2400")
    await evaluate('scrollTo(0,0);true')
    await wait(first_frame)
    assert await evaluate('writes.slice(1).every((time,i)=>time-writes[i]>=118)'),await evaluate('writes')

    # Simulate the visibility event deterministically in headless Chromium.
    await evaluate("""window.visibilityForTest=true;
      Object.defineProperty(document,'hidden',{configurable:true,get:()=>visibilityForTest});
      document.dispatchEvent(new Event('visibilitychange'));window.beforeHide=writes.length;
      scrollTo(0,elList.offsetTop+heightBefore(41));true""")
    await wait('!windowBusy && visibleAnchor(0)?.id>10000')
    assert await evaluate('writes.length===beforeHide')
    await evaluate("visibilityForTest=false;document.dispatchEvent(new Event('visibilitychange'));true")
    await wait(correct)
    assert await evaluate(f'{src}!==DATA.vod_preview.frames[0][1]')
    await evaluate("elQ.value='unique-desktop-storyboard-miss';elQ.dispatchEvent(new Event('input'));true")
    await wait(f'!FILTERING && !windowBusy && rowCount()===0 && !({src})')
    await evaluate("elQ.value='';elU.value=decodeMessage(rows[rows.length-1]).user;true")
    await evaluate("elU.dispatchEvent(new Event('input'));true")
    await wait('!FILTERING && !windowBusy && rowCount()>0 && rowCount()<rows.length')
    await wait(correct)
    await evaluate("elU.value='';elU.dispatchEvent(new Event('input'));true")
    await wait(first_frame)

    mobile_geometry="""[elHeader,elList,elScrubber,elPanelHandle].map(n=>{
      const r=n.getBoundingClientRect();return [r.x,r.y,r.width,r.height];
    })"""
    async def mobile_modes():
        await wait('!windowBusy && !panelAnimating')
        result=[await evaluate(mobile_geometry)]
        await evaluate('elPanelHandle.click();true')
        await wait('PANEL_EXPANDED && !panelAnimating && !windowBusy')
        result.append(await evaluate(mobile_geometry))
        await evaluate('elStoryboardToggle.click();true')
        await wait('storyboardMode && elStoryboardFrame.naturalWidth===220')
        result.append(await evaluate(mobile_geometry))
        await evaluate('elPanelHandle.click();true')
        assert await evaluate('PANEL_EXPANDED && !storyboardMode')
        return result
    await resize(900)
    await wait('MOBILE_MQ.matches')
    assert await evaluate(f'{image}.parentElement.hidden && !({src})')
    await resize(901)
    await wait(first_frame)
    await load('base',390,720,mobile=True)
    original_mobile=await mobile_modes()
    await load('new',390,720,mobile=True)
    assert await mobile_modes()==original_mobile

    measurements=[]
    for version in ('base','new'):
        await load(version,1440)
        if version=='new':
            await wait(first_frame)
        sample=await evaluate("""(async()=>{
          const samples=[], lookupTimes=[];
          const select=nearestStoryFrame;
          nearestStoryFrame=(...args)=>{lookupTimes.push(performance.now());return select(...args);};
          let inlineLookups=0;
          for(const block of [0,1,5,14,26,41,16,1]){
            const before=lookupTimes.length,start=performance.now();
            scrollTo(0,elList.offsetTop+heightBefore(block));
            document.dispatchEvent(new Event('scroll'));
            inlineLookups+=lookupTimes.length-before;
            await new Promise(requestAnimationFrame);
            samples.push(performance.now()-start);
            await new Promise(resolve=>setTimeout(resolve,175));
          }
          return {samples,lookups:lookupTimes.length,inlineLookups};
        })()""")
        assert sample['inlineLookups']==0
        assert (sample['lookups']>0)==(version=='new'),sample
        measurements.append(dict(version=version,scroll_to_raf_max_ms=round(max(sample['samples']),1),
                                 scroll_to_raf_median_ms=round(median(sample['samples']),1),
                                 lookups=sample['lookups'],inline_lookups=sample['inlineLookups']))
    assert not errors,errors
    assert not any('/storyboards/' in url or '/cf_vods/test/' in url for url in requests),requests
    print(json.dumps(dict(desktop_storyboard='PASS',viewports=[901,1024,1440],rows=count,
                          comparison=measurements,offline=True)),flush=True)
