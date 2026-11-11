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
            && box.bottom<=innerHeight-7 && Math.abs(box.width/box.height-220/124)<.05 && %s;
        })()""" % (frame, 'box.bottom<=bar.top-5' if expanded else 'box.top>=bar.bottom+5')

    sizes=[]
    for width,height in ((320,568),(390,720),(568,320)):
        if width==568:
            await resize(width,height)
            await wait("innerWidth===568 && !windowBusy")
            await evaluate("syncPanelState(false,false);scrollTo(0,0);true")
            await wait("!windowBusy && scrubIndex===0")
        else:
            await load(width,height)
        dimensions=[]
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
            box = await evaluate("""(()=>{
              const r=document.querySelector('.scrub-preview').getBoundingClientRect();
              const bar=elScrubber.getBoundingClientRect();
              return [r.width,r.height,r.left,r.top,bar.top];
            })()""")
            assert abs(box[2]+box[0]/2-width/2)<1, box
            dimensions.append(box[:2])
            stops = ((.5,1),(1,2)) if args.context else ((.45,0),(.65,1),(1,2))
            for fraction,frame in stops:
                await touch('touchMove',dict(x=start['x']+track['width']*fraction,y=start['y']))
                await wait(position_ok(expanded,frame))
                assert await evaluate("""(()=>{
                  const r=document.querySelector('.scrub-preview').getBoundingClientRect();
                  return [r.width,r.height,r.left,r.top];
                })()""")==box[:4]
            if expanded:
                compact_bar_bottom=box[4]-await evaluate('elPanelDetails.getBoundingClientRect().height')+geometry[2]
                expected=int(min(width-16,min(box[4]-14,height-14-compact_bar_bottom)*220/124))
                assert box[0]==expected,(box,expected)
                sizes.append(dict(viewport=[width,height],expanded_bar_top=box[4],preview=box[:2]))
            assert await evaluate("[elHeader.offsetHeight,elList.offsetTop,elScrubber.offsetHeight]")==geometry
            await touch('touchEnd')
            await wait("!document.querySelector('.scrub-preview') && !scrubActive() && !windowBusy")
            assert await evaluate("elList.querySelectorAll('.row').length<=2400")
        assert dimensions[0]==dimensions[1],dimensions

    await load(390,150)
    point=await evaluate("""(()=>{
      const r=elScrubber.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2};
    })()""")
    await touch('touchStart',point)
    await wait('scrubGesture && !scrubPreviewQueued')
    assert await evaluate("!document.querySelector('.scrub-preview')")
    await touch('touchEnd')

    geometry = """[elHeader,elVodCard,elList].map(n=>{const r=n.getBoundingClientRect();return [r.x,r.y,r.width,r.height];})"""
    await load(1440,900,'base')
    desktop = await evaluate(geometry)
    await load(1440,900)
    assert await evaluate("getComputedStyle(elScrubber).display==='none' || !elScrubber.getClientRects().length")
    assert await evaluate(geometry)==desktop

    await call('Performance.enable')
    comparisons=[]
    for version,enabled in (('base',False),('base',True),('new',False),('new',True)):
        await load(390,720,version)
        if not enabled:
            await evaluate('DATA.vod_preview.frames.length=0')
        await evaluate("""(()=>{
          window.previewStats={geometry:0,thumb:0,src:0,lookups:0,lookupMs:0,windows:0,windowWallMs:0};
          const show=showWindow;
          showWindow=async(...args)=>{
            const start=performance.now();previewStats.windows++;
            try{return await show(...args);}
            finally{previewStats.windowWallMs+=performance.now()-start;}
          };
          const rect=Element.prototype.getBoundingClientRect;
          Element.prototype.getBoundingClientRect=function(){
            if(this.classList.contains('scrub-thumb')) previewStats.thumb++;
            return rect.call(this);
          };
          if(typeof measureScrubPreview==='function'){
            const measure=measureScrubPreview;
            measureScrubPreview=()=>{previewStats.geometry++;return measure();};
          }
          const pick=nearestStoryFrame;
          nearestStoryFrame=(...args)=>{
            const start=performance.now(),value=pick(...args);
            previewStats.lookupMs+=performance.now()-start;previewStats.lookups++;return value;
          };
          const desc=Object.getOwnPropertyDescriptor(HTMLImageElement.prototype,'src');
          Object.defineProperty(HTMLImageElement.prototype,'src',{...desc,set(value){
            if(this.classList.contains('scrub-preview')) previewStats.src++;
            desc.set.call(this,value);
          }});
          return true;
        })()""")
        track=await evaluate("""(()=>{
          const r=elScrubber.querySelector('.scrub-track').getBoundingClientRect();
          return {x:r.left,width:r.width,y:r.top+r.height/2};
        })()""")
        before={m['name']:m['value'] for m in (await call('Performance.getMetrics'))['metrics']}
        await touch('touchStart',dict(x=track['x'],y=track['y']))
        for step in range(1,25):
            await touch('touchMove',dict(x=track['x']+track['width']*step/24,y=track['y']))
        await touch('touchEnd')
        await wait('!windowBusy && !scrubActive() && scrubIndex===rowCount()-1')
        after={m['name']:m['value'] for m in (await call('Performance.getMetrics'))['metrics']}
        comparisons.append(dict(version=version,preview=enabled,**(await evaluate('previewStats')),
                                layoutMs=round(1000*(after['LayoutDuration']-before['LayoutDuration']),1),
                                scriptMs=round(1000*(after['ScriptDuration']-before['ScriptDuration']),1)))
    assert comparisons[1]['thumb']>1 and comparisons[3]['thumb']==0 and comparisons[3]['geometry']==1,comparisons
    assert comparisons[3]['src']==3,comparisons
    decoder=await evaluate("""(async()=>{
      const times=[];
      for(const [,src] of DATA.vod_preview.frames){
        const bytes=Uint8Array.from(atob(src.split(',')[1]),char=>char.charCodeAt(0));
        const url=URL.createObjectURL(new Blob([bytes],{type:'image/jpeg'}));
        try{
          const img=new Image(),start=performance.now();img.src=url;await img.decode();
          times.push(performance.now()-start);
          if(img.naturalWidth!==220 || img.naturalHeight!==124) throw Error('Frame changed size');
        }finally{URL.revokeObjectURL(url);}
      }
      return times;
    })()""")
    assert not any('/storyboards/' in url or '/cf_vods/test/' in url for url in requests), requests
    assert not errors, errors
    print(json.dumps(dict(storyboard='PASS', offline=True, sizes=sizes, desktop=1440,
                          comparison=comparisons, jpegDecodeMs=decoder)))
