"""Exercise native touch input and decoded inline preview images in Chromium."""
import json
import random

from PIL import Image
from src.twitch_api.storyboard import jpeg


def preview_fixture(count):
    size = (220, 124)
    rng = random.Random(79)
    noise = Image.frombytes('RGB', size, rng.randbytes(size[0]*size[1]*3))
    images = [jpeg(Image.blend(Image.new('RGB', size, color), noise, .45), None, 128*1024, quality=92)
              for color in ('red', 'green', 'blue')]
    return dict(duration_seconds=count, thumbnail_preview_url=(
        'https://static-cdn.jtvnw.net/cf_vods/test/thumb.jpg'),
        preview_assets=dict(cover=images[1], aspect=list(size),
                            frames=list(zip((0, count // 2, count - 1), images))))


async def check_storyboard(call, evaluate, wait, out, args, errors, requests):
    count = args.sizes[0]
    requests.clear()
    errors.clear()
    await call('Network.enable')
    await call('Network.emulateNetworkConditions', dict(
        offline=True, latency=0, downloadThroughput=0, uploadThroughput=0))

    async def resize(width, height):
        await call('Emulation.setDeviceMetricsOverride', dict(
            width=width, height=height, deviceScaleFactor=1, mobile=width<900))
        await call('Emulation.setTouchEmulationEnabled', dict(enabled=width<900, maxTouchPoints=1))

    async def load(width, height, version='new'):
        await resize(width, height)
        await call('Page.navigate', dict(url=(out/f'{version}-{count}-offline.html').as_uri()))
        await wait(f'innerWidth==={width} && innerHeight==={height}')
        await wait("document.readyState==='complete' && !windowBusy && !FILTERING && rowCount()>1")
        await wait("elVodPreviewImg.complete && elVodPreviewImg.naturalWidth>0")
        assert await evaluate('elVodPreviewImg.src===DATA.vod_preview.cover')

    async def touch(event, x=None, y=None):
        await call('Input.dispatchTouchEvent', dict(
            type=event, touchPoints=[dict(x=x,y=y)] if x is not None else []))

    async def track():
        return await evaluate("""(()=>{
          const r=elScrubber.querySelector('.scrub-track').getBoundingClientRect();
          return [r.left,r.right,r.top+r.height/2];
        })()""")

    async def drag(forward, end='touchEnd', steps=1):
        left,right,y=await track()
        start,stop=(left,right) if forward else (right,left)
        await touch('touchStart',start,y)
        for step in range(1,steps+1):
            await touch('touchMove',start+(stop-start)*step/steps,y)
        await touch(end)
        await wait('!scrubActive() && !windowBusy')

    async def expand():
        await evaluate('elPanelHandle.click();true')
        await wait('PANEL_EXPANDED && !panelAnimating && !windowBusy')

    geometry = '[elHeader.offsetHeight,elList.offsetTop,elScrubber.getBoundingClientRect().top,scrollY]'
    frame_box = """(()=>{
      const r=elStoryboardFrame.getBoundingClientRect();
      return [r.x,r.y,r.width,r.height];
    })()"""
    def frame_is(index):
        return f"""!elStoryboardFrame.hidden && elStoryboardFrame.complete
          && elStoryboardFrame.naturalWidth===220 && elStoryboardFrame.naturalHeight===124
          && elStoryboardFrame.src===DATA.vod_preview.frames[{index}][1]"""

    sizes=[]
    for width,height in ((320,568),(390,720)):
        await load(width,height)
        await evaluate("""(()=>{
          window.storyPaint=[];
          const paint=paintStoryboardFrame;
          paintStoryboardFrame=()=>{storyPaint.push(performance.now());paint();};
          return true;
        })()""")
        assert await evaluate("elPanelDetails.inert && !document.querySelector('.scrub-preview')")
        await drag(True)
        assert await evaluate('scrubIndex===rowCount()-1 && !elStoryboardFrame.hasAttribute("src")')
        await expand()
        await wait("elStoryboardToggle.getBoundingClientRect().width>0")
        await drag(False)
        assert await evaluate('scrubIndex===0 && !elStoryboardFrame.hasAttribute("src") && storyPaint.length===0')
        await evaluate("elQ.value='preserved';elToggleTime.click();requestScrub(Math.round((rowCount()-1)*.72));true")
        await wait('!scrubActive() && !windowBusy && !FILTERING')
        assert await evaluate("elList.querySelectorAll('.time').length===0")
        before=await evaluate(geometry)
        await evaluate('elStoryboardToggle.click();true')
        await wait(frame_is(1))
        assert await evaluate(geometry)==before
        assert await evaluate("""elPanelContent.hidden && !elPanelContent.getClientRects().length
          && getComputedStyle(elPanelHandle.querySelector('.panel-menu')).display!=='none'
          && getComputedStyle(elPanelHandle.querySelector('.panel-chevron')).display==='none'""")
        box=await evaluate(frame_box)
        x,y,w,h=box
        assert abs(x+w/2-width/2)<1 and y>=8 and y+h<=before[2]-5, (box,before)
        assert abs(w/h-220/124)<.05
        sizes.append(dict(viewport=[width,height],frame=[w,h],slider_y=before[2]))
        await drag(True,steps=24)
        await wait(frame_is(2))
        assert await evaluate(frame_box)==box
        assert await evaluate("scrubIndex===rowCount()-1 && elList.querySelectorAll('.row').length<=2400")
        assert await evaluate("storyPaint.slice(2).every((time,i)=>time-storyPaint[i+1]>=120)"), await evaluate('storyPaint')
        await drag(False,end='touchCancel')
        await wait(frame_is(0))
        assert await evaluate('scrubIndex===0')
        await evaluate("""window.frameWrites=[];
          new MutationObserver(records=>frameWrites.push(...records))
            .observe(elStoryboardFrame,{attributeFilter:['src']});
          requestScrub(Math.round((rowCount()-1)*.20));true""")
        await wait('!scrubActive() && !storyboardTimer && !storyboardIdle')
        assert await evaluate(f'{frame_is(0)} && frameWrites.length===0')
        await evaluate("""requestScrub(Math.round((rowCount()-1)*.72));
          requestScrub(rowCount()-1);true""")
        await wait(f'{frame_is(2)} && !scrubActive() && !storyboardTimer && !storyboardIdle')
        assert await evaluate('frameWrites.length===1')
        before=await evaluate(geometry)
        await evaluate("""requestScrub(0);elPanelHandle.click();
          window.paintAtExit=storyPaint.length;true""")
        await wait('!scrubActive() && !windowBusy')
        await evaluate('new Promise(resolve=>setTimeout(()=>resolve(true),280))')
        assert await evaluate("""PANEL_EXPANDED && !storyboardMode && !storyboardTimer && !storyboardIdle
          && !elStoryboardFrame.hasAttribute('src') && storyPaint.length===paintAtExit
          && !elPanelContent.hidden && elQ.value==='preserved'
          && elList.querySelectorAll('.time').length===0
          && getComputedStyle(elPanelHandle.querySelector('.panel-chevron')).display!=='none'""")
        assert (await evaluate(geometry))[:3]==before[:3]
        await evaluate('elPanelHandle.click();true')
        await wait('!PANEL_EXPANDED && !panelAnimating && elPanelDetails.inert')

    await load(390,720)
    await expand()
    await evaluate('elStoryboardToggle.click();true')
    for width,height in ((568,320),(390,100),(390,720)):
        await resize(width,height)
        await wait(f'innerWidth==={width} && innerHeight==={height} && !windowBusy')
        assert await evaluate("""storyboardMode && elScrubber.getBoundingClientRect().bottom<=innerHeight
          && (elStoryboardFrame.hidden || elStoryboardFrame.getBoundingClientRect().bottom
             <=elScrubber.getBoundingClientRect().top-5)""")
    await wait(frame_is(0))
    await load(390,720,'no-storyboard')
    await expand()
    assert await evaluate("elStoryboardToggle.hidden && !elStoryboardToggle.getClientRects().length")
    await drag(True)
    assert await evaluate('scrubIndex===rowCount()-1')

    desktop_geometry = """[elHeader,elVodCard,elList].map(n=>{
      const r=n.getBoundingClientRect();return [r.x,r.y,r.width,r.height];
    })"""
    await load(1440,900,'base')
    desktop=await evaluate(desktop_geometry)
    await load(1440,900)
    assert await evaluate("!elScrubber.getClientRects().length && !elStoryboardToggle.getClientRects().length")
    assert await evaluate(desktop_geometry)==desktop

    comparisons=[]
    for version in ('base','new'):
        await load(390,720,version)
        if version=='base':
            await evaluate('DATA.vod_preview.frames.length=0;true')
        await evaluate("""(()=>{
          window.timings={handler:[],commit:[],storyboardLookups:0,release:0};
          let lastInput;
          const move=pointerScrub,request=requestScrub,remember=rememberScrubPosition;
          const pick=nearestStoryFrame;
          nearestStoryFrame=(...args)=>{timings.storyboardLookups++;return pick(...args);};
          pointerScrub=(...args)=>{
            const start=performance.now();move(...args);timings.handler.push(performance.now()-start);
          };
          requestScrub=(...args)=>{lastInput=performance.now();request(...args);};
          rememberScrubPosition=(...args)=>{
            remember(...args);
            if(scrubRunning && lastInput!==undefined) timings.commit.push(performance.now()-lastInput);
          };
          elScrubber.addEventListener('pointerup',()=>{timings.release=performance.now();},true);
          return true;
        })()""")
        await drag(True,steps=24)
        stats=await evaluate('timings')
        assert stats['storyboardLookups']==0 and stats['release'] and stats['commit'],stats
        assert await evaluate("scrubIndex===rowCount()-1 && elList.querySelectorAll('.row').length<=2400")
        samples=sorted(stats['handler'])
        comparisons.append(dict(version=version,handler_p95_ms=round(samples[int(.95*(len(samples)-1))],2),
                                last_input_to_commit_ms=round(stats['commit'][-1],1),
                                storyboard_lookups=stats['storyboardLookups']))
    assert not any('/storyboards/' in url or '/cf_vods/test/' in url for url in requests), requests
    assert not errors,errors
    file_sizes={version:(out/f'{version}-{count}-offline.html').stat().st_size
                for version in ('base','new','no-storyboard')}
    print(json.dumps(dict(storyboard='PASS',offline=True,sizes=sizes,desktop=1440,
                          comparison=comparisons,html_bytes=file_sizes)),flush=True)
