"""Focused synthetic marker geometry/raster checks on the existing CDP harness."""
import asyncio
import base64
import io
import json

from PIL import Image


async def check_marker(call, evaluate, wait, out, args, errors, requests):
    results=[];baseline_thumbs={};desktop=[]

    async def settled():
        await wait('!FILTERING && !panelAnimating && !windowBusy && !scrubActive() && !messageSelectionFrame')
        await asyncio.sleep(.12)

    async def size(width):
        await call('Emulation.setDeviceMetricsOverride',dict(width=width,height=900,deviceScaleFactor=1,mobile=width<600))
        await asyncio.sleep(.15)

    async def load(version,width,mode):
        await size(width);errors.clear()
        url=(out/f'{version}-10000-{mode}.html').as_uri()
        await call('Page.navigate',dict(url=url))
        await wait('location.href==='+json.dumps(url))
        await wait("document.readyState==='complete' && typeof scrubActive==='function' && !windowBusy")
        await settled()
        await evaluate('window.scrollTo(0,0);true');await settled()
        await wait("elList.querySelector('.row')!==null")

    async def shot(name,clip=None):
        params=dict(format='png')
        if clip:params['clip']=dict(**clip,scale=1)
        data=base64.b64decode((await call('Page.captureScreenshot',params))['data'])
        if name:(out/(name+'.png')).write_bytes(data)
        return Image.open(io.BytesIO(data)).convert('RGB')

    geometry="""(()=>{const rect=n=>{const r=n.getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height,right:r.right}};
const thumb=elScrubber.querySelector('.scrub-thumb'),labels=[...elScrubber.querySelectorAll('.scrub-label')];
return {outer:rect(elScrubber),track:rect(elScrubber.querySelector('.scrub-track')),fill:rect(elScrubber.querySelector('.scrub-fill')),thumb:rect(thumb),
colors:[getComputedStyle(thumb).backgroundColor,getComputedStyle(thumb).backgroundImage],labels:labels.map(n=>({rect:rect(n),clip:getComputedStyle(n).backgroundClip,color:getComputedStyle(n).color,z:getComputedStyle(n).zIndex})),
text:rect(elScrubber.querySelector('.scrub-times')),label:elScrubber.getAttribute('aria-valuetext')};})()"""

    for width in (320,390):
        for mode in ('online','offline'):
            for version in ('base','new'):
                await load(version,width,mode)
                for expanded in (False,True):
                    await evaluate(f'syncPanelState({str(expanded).lower()});true');await settled()
                    for percent in (0,1,25,50,75,99,100):
                        await evaluate(f'setScrubIndex(Math.round({percent}/100*(rowCount()-1)));true')
                        if expanded:
                            await evaluate("elScrubber.querySelectorAll('.scrub-times').forEach(n=>n.textContent='1234567:59:59 / 1234567:59:59');fitScrubLabel();true")
                        # Isolate paint geometry from logical-row rounding and scroll hysteresis.
                        await evaluate(f"elScrubber.style.setProperty('--scrub-progress','{percent}%');elScrubber.style.setProperty('--scrub-ratio','{percent/100}');true")
                        g=await evaluate(geometry);t=g['thumb'];o=g['outer'];r=g['track']
                        key=(width,mode,expanded,percent)
                        if version=='base':baseline_thumbs[key]=t
                        if version=='new':
                            old=baseline_thumbs[key]
                            assert abs(t['x']-old['x'])<.08 and t['w']==old['w'] and t['h']==old['h'],(g,old)
                            center=t['x']+t['w']/2
                            assert abs(center-(r['x']+r['w']*percent/100))<.08,g
                            assert abs(g['fill']['right']-center)<.08,g
                            assert abs(center-(o['x']+11+(o['w']-22)*percent/100))<.08,g
                            assert t['w']==18 and t['h']==46 and g['colors']==['rgb(160, 163, 183)','none'],g
                            assert len(g['labels'])==1 and g['labels'][0]['color']=='rgba(0, 0, 0, 0)',g
                            assert g['text']['w']<=r['w']-8+.1,g
                            # The single glyph mask switches palette at the capsule's right edge.
                            edge=await evaluate("(()=>{const n=document.createElement('i');n.style.cssText='position:absolute;width:var(--scrub-label-edge);height:0';elScrubber.append(n);const w=n.getBoundingClientRect().width;n.remove();return w})()")
                            assert abs(edge-(t['right']-o['x']))<.1,g
                            assert all(n['clip']=='text' and int(n['z'])>1 for n in g['labels']),g
                            # Sample all across the capsule at mid-height outside the glyph band.
                            clip=dict(x=o['x'],y=o['y'],width=o['w'],height=o['h'])
                            raster=await shot(None,clip)
                            y=12
                            for x in range(round(t['x']-o['x'])+3,round(t['right']-o['x'])-3):
                                assert raster.getpixel((x,y))==(160,163,183),(width,percent,x,raster.getpixel((x,y)))
                            if percent in (25,50,75):
                                # Raster proof: every well-covered reference glyph pixel must
                                # retain visible contrast, even where the capsule crosses it.
                                await evaluate("elScrubber.querySelectorAll('.scrub-label').forEach(n=>n.style.visibility='hidden');true")
                                background=await shot(None,clip)
                                await evaluate("(()=>{elScrubber.querySelectorAll('.scrub-label').forEach(n=>n.style.visibility='');const n=document.createElement('style');n.id='marker-probe';n.textContent='#fastScrubber,#fastScrubber .scrub-track,#fastScrubber .scrub-fill,#fastScrubber .scrub-thumb{background:#000!important;border-color:#000!important}#fastScrubber .scrub-label{visibility:visible;background:none;color:#fff;clip-path:none}';document.head.append(n);return true})()")
                                reference=await shot(None,clip)
                                await evaluate("document.getElementById('marker-probe').remove();true")
                                glyphs=0
                                for yy in range(reference.height):
                                    for xx in range(reference.width):
                                        if min(reference.getpixel((xx,yy)))>180:
                                            glyphs+=1
                                            contrast=max(abs(a-b) for a,b in zip(raster.getpixel((xx,yy)),background.getpixel((xx,yy))))
                                            if contrast<=40:
                                                raster.save(out/'text-probe-actual.png');reference.save(out/'text-probe-reference.png');background.save(out/'text-probe-background.png')
                                            assert contrast>40,(width,percent,xx,yy,contrast,raster.getpixel((xx,yy)),background.getpixel((xx,yy)))
                                assert glyphs>50,glyphs
                            results.append(dict(width=width,mode=mode,expanded=expanded,percent=percent,geometry='PASS',solid_capsule='PASS'))
                        if mode=='offline' and (width==390 or percent in (0,50,100)):
                            await shot(f'{version}-{width}-{int(expanded)}-{percent}')
                    assert not errors,errors
                if version=='new':
                    # Actual logical navigation, drag, resize and physical end, not just CSS probes.
                    await evaluate('requestScrub(0);true');await settled()
                    await evaluate('elScrubber.focus();true')
                    await call('Input.dispatchKeyEvent',dict(type='keyDown',key='ArrowRight'))
                    await settled();assert await evaluate('scrubIndex')>0
                    await evaluate('requestScrub(5000);true');await settled()
                    g=await evaluate(geometry);x=g['thumb']['x']+9;y=g['outer']['y']+23
                    await call('Input.dispatchMouseEvent',dict(type='mousePressed',button='left',buttons=1,clickCount=1,x=x,y=y))
                    await call('Input.dispatchMouseEvent',dict(type='mouseMoved',button='left',buttons=1,x=x+10,y=y))
                    await call('Input.dispatchMouseEvent',dict(type='mouseReleased',button='left',buttons=0,clickCount=1,x=x+10,y=y))
                    await settled();assert await evaluate('scrubIndex')>5000
                    await size(390 if width==320 else 320);await settled()
                    await evaluate('requestScrub(rowCount()-1);true');await settled()
                    assert await evaluate('scrubIndex===rowCount()-1 && +elScrubber.getAttribute("aria-valuenow")===100')
                    await evaluate('window.scrollTo(0,0);true');await settled()
                    assert await evaluate('scrubIndex')<100
    for version in ('base','new'):
        await load(version,1440,'offline')
        assert await evaluate('getComputedStyle(document.querySelector(".mobile-control-row")).display')=='none'
        desktop.append(await evaluate("[elHeader,elList].map(n=>{const r=n.getBoundingClientRect();return [r.x,r.y,r.width,r.height]})"))
        await shot(version+'-desktop')
    assert desktop[0]==desktop[1],desktop
    # GPU rounded-border antialiasing may differ by one RGB level between paints.
    from PIL import ImageChops
    diff=ImageChops.difference(Image.open(out/'base-desktop.png'),Image.open(out/'new-desktop.png'))
    assert all(hi<=1 for lo,hi in diff.getextrema()),'Desktop rendering changed'
    (out/'marker-results.json').write_text(json.dumps(results,indent=2))
    print(json.dumps(dict(marker_cases=len(results),desktop='unchanged',navigation_resize_end='PASS')),flush=True)
