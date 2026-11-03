"""Small image-only cases on the existing browser harness; no benchmarks."""
import base64
import json
from src.html_renderer import render_viewer_html

async def check_images(call,evaluate,wait,out,args,errors,requests):
    results=[]
    for mode in ('online','offline'):
        for live in (False,True):
            page=out/f'image-{mode}-{live}.html'
            page.write_text(render_viewer_html([{'user':'synthetic','text':'safe <script> text','offset':0}],
                'Synthetic title','channel','https://www.twitch.tv/videos/123',mode=mode,captured_during_live=live),encoding='utf-8')
            for width in args.widths:
                errors.clear()
                await call('Emulation.setDeviceMetricsOverride',dict(width=width,height=900,deviceScaleFactor=1,mobile=width<600))
                await call('Page.navigate',dict(url=page.as_uri()))
                await wait("document.readyState==='complete' && elVodPreviewImg.complete && elVodPreviewImg.naturalWidth===720 && !windowBusy")
                for expanded in (False,True) if width<600 else (True,):
                    await evaluate('syncPanelState('+str(expanded).lower()+',false);true')
                    await wait('!panelAnimating && !windowBusy')
                    geometry=await evaluate("(()=>{const r=elVodPreviewImg.getBoundingClientRect();return {w:r.width,h:r.height,overflow:document.documentElement.scrollWidth>innerWidth}})()")
                    assert not geometry['overflow'] and abs(geometry['w']/geometry['h']-16/9)<.05,geometry
                    shot=await call('Page.captureScreenshot',dict(format='png'))
                    (out/f'image-{mode}-{live}-{width}-{expanded}.png').write_bytes(base64.b64decode(shot['data']))
                await evaluate('elVodCard.click();true')
                await wait('vodOverlay!==null')
                await evaluate("vodOverlay.click();true")
                assert await evaluate("vodOverlay===null && document.body.style.overflow===''"),errors
                assert not errors,errors
                results.append(dict(mode=mode,live=live,width=width,status='PASS',geometry=geometry))
    (out/'image-results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
    print(json.dumps(dict(image_cases=len(results),status='PASS')))
