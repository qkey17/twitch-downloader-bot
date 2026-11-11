"""Exercise native touch input and decoded inline preview images in Chromium."""
import json

from PIL import Image
from src.twitch_api.storyboard import jpeg


def preview_fixture(count):
    colors = ('red', 'green', 'blue')
    images = [jpeg(Image.new('RGB', (144, 81), color), (144, 81), 16384) for color in colors]
    return dict(duration_seconds=count, thumbnail_preview_url=(
        'https://static-cdn.jtvnw.net/cf_vods/test/thumb.jpg'),
        preview_assets=dict(cover=images[1], frames=list(zip((0, count // 2, count - 1), images))))


async def check_storyboard(call, evaluate, wait, out, args, errors, requests):
    count = args.sizes[0]
    requests.clear()
    errors.clear()
    await call('Network.enable')
    await call('Network.emulateNetworkConditions', dict(
        offline=True, latency=0, downloadThroughput=0, uploadThroughput=0))

    async def load(width, height):
        await call('Emulation.setDeviceMetricsOverride', dict(
            width=width, height=height, deviceScaleFactor=1, mobile=width<900))
        await call('Emulation.setTouchEmulationEnabled', dict(enabled=width<900, maxTouchPoints=1))
        await call('Page.navigate', dict(url=(out/f'new-{count}-offline.html').as_uri()))
        await wait("document.readyState==='complete' && !windowBusy && !FILTERING && rowCount()>1")
        await wait("elVodPreviewImg.complete && elVodPreviewImg.naturalWidth>0")
        assert await evaluate("elVodPreviewImg.src===DATA.vod_preview.cover && !document.querySelector('.scrub-preview')")

    async def touch(event, point=None):
        await call('Input.dispatchTouchEvent', dict(type=event, touchPoints=[point] if point else []))

    def position_ok(expanded, frame):
        return """(()=>{
          const img=document.querySelector('.scrub-preview'), bar=elScrubber.getBoundingClientRect();
          if(!img?.complete || !img.naturalWidth || img.src!==DATA.vod_preview.frames[%d][1]) return false;
          const box=img.getBoundingClientRect();
          return !img.hidden && box.left>=7 && box.right<=innerWidth-7 && box.top>=7
            && box.bottom<=innerHeight-7 && Math.abs(box.width/box.height-16/9)<.08 && %s;
        })()""" % (frame, 'box.bottom<=bar.top-5' if expanded else 'box.top>=bar.bottom+5')

    for width,height in ((320,568),(390,720)):
        await load(width,height)
        for expanded in (False,True):
            if expanded:
                await evaluate("elPanelHandle.click();true")
                await wait("!panelAnimating && elHeader.classList.contains('is-expanded')")
                await evaluate('scrollTo(0,0);true')
                await wait('!windowBusy && scrubIndex===0')
            track=await evaluate("""(()=>{
              const r=elScrubber.querySelector('.scrub-track').getBoundingClientRect();
              return {left:r.left,width:r.width,y:r.top+r.height/2};
            })()""")
            start=dict(x=track['left'],y=track['y'])
            geometry=await evaluate("[elHeader.offsetHeight,elList.offsetTop,elScrubber.offsetHeight]")
            await touch('touchStart', start)
            await wait(position_ok(expanded,0))
            stops = ((.5,1),(1,2)) if args.context else ((.45,0),(.65,1),(1,2))
            for fraction,frame in stops:
                await touch('touchMove',dict(x=start['x']+track['width']*fraction,y=start['y']))
                await wait(position_ok(expanded,frame))
            assert await evaluate("[elHeader.offsetHeight,elList.offsetTop,elScrubber.offsetHeight]")==geometry
            await touch('touchEnd')
            await wait("!document.querySelector('.scrub-preview') && !scrubActive() && !windowBusy")
            assert await evaluate("elList.querySelectorAll('.row').length<=2400")

    await load(1440,900)
    assert await evaluate("getComputedStyle(elScrubber).display==='none' || !elScrubber.getClientRects().length")
    assert not any('/storyboards/' in url or '/cf_vods/test/' in url for url in requests), requests
    assert not errors, errors
    print(json.dumps(dict(storyboard='PASS', offline=True, compact_and_expanded=[320,390], desktop=1440)))
