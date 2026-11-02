"""Focused notice checks on the existing synthetic, network-intercepted CDP stand."""
import asyncio
import base64
import json


async def check_notices(call, evaluate, wait, out, args, errors, requests):
    results = []

    async def settled():
        await wait('!FILTERING && !panelAnimating && !windowBusy && !scrubActive() && !scrubFrame && !scrubSyncFrame')
        await asyncio.sleep(.15)
        await wait('!windowBusy')

    async def shot(name):
        data = await call('Page.captureScreenshot', dict(format='png'))
        (out / (name+'.png')).write_bytes(base64.b64decode(data['data']))

    async def query(text='', user=''):
        await evaluate('elQ.value='+json.dumps(text)+';elU.value='+json.dumps(user)+';applyFilters();true')
        await settled()

    async def jump(index, width, count):
        if width < 600:
            await evaluate(f'requestScrub({index});true')
        else:
            # Desktop intentionally has no mobile scrubber; use document scrolling.
            await evaluate(f'window.scrollTo(0,(document.documentElement.scrollHeight-innerHeight)*{index/(count-1)});true')
        await settled()

    for width in args.widths:
        await call('Emulation.setDeviceMetricsOverride', dict(width=width, height=1000, deviceScaleFactor=1, mobile=width<600))
        await call('Emulation.setTouchEmulationEnabled', dict(enabled=width<600, maxTouchPoints=1))
        for count in args.sizes:
            for mode in ('offline', 'online'):
                errors.clear(); requests.clear()
                await call('Page.navigate', dict(url=(out/f'new-{count}-{mode}.html').as_uri()))
                await wait("document.readyState==='complete' && typeof windowBusy!=='undefined' && !windowBusy")
                await settled()
                assert await evaluate('rowCount()') == count
                assert await evaluate('''(()=>{const n=[...elList.querySelectorAll('.row')];return n.length<=2400 && n.every((r,i)=>+r.dataset.id===i) && n.slice(0,16).every(r=>r.classList.contains('notice'))})()''')
                assert await evaluate("[...elList.querySelectorAll('.row')].slice(0,80).every(n=>n.textContent===selectionText(rowAt(+n.dataset.id)))"), 'DOM/partial-copy projection mismatch'
                assert await evaluate("[0,2,6,7,10,11,12,13,14,15].every(i=>elList.querySelector(`.row[data-id='${i}'] .notice-comment`))")
                assert await evaluate("[1,3,4,5,8,9].every(i=>!elList.querySelector(`.row[data-id='${i}'] .notice-comment`))")
                assert await evaluate("elList.querySelectorAll('.row[data-id=\"10\"] .notice-comment img.emote').length===1")
                assert await evaluate("elList.querySelector('.row[data-id=\"9\"] .notice-system').title.includes('STVOLove')")
                assert await evaluate("elList.querySelector('.row[data-id=\"11\"]').textContent.includes('<script>') && !window.fixtureXSS && !elList.querySelector('script')")
                assert await evaluate("elList.querySelector('.row[data-id=\"4\"]').classList.contains('notice-community_gift') && elList.querySelector('.row[data-id=\"5\"]').classList.contains('notice-gift')")
                assert await evaluate('document.documentElement.scrollWidth<=innerWidth'), ('overflow', width)
                if count == min(args.sizes) and mode == 'offline':
                    await shot(f'notices-main-{width}')
                    await evaluate("elList.querySelector('.row[data-id=\"6\"]').scrollIntoView();true")
                    await settled(); await shot(f'notices-more-{width}')
                    await evaluate("elList.querySelector('.row[data-id=\"15\"]').scrollIntoView();true")
                    await settled(); await shot(f'notices-long-{width}')
                    assert await evaluate('document.documentElement.scrollWidth<=innerWidth')
                # Both halves remain searchable using full original text.
                for text in ('sparked a watch streak', 'я новый 240'):
                    await query(text)
                    assert await evaluate('rowCount()>0 && Array.from({length:rowCount()},(_,i)=>rowAt(i)).every(r=>r.text.includes('+json.dumps(text)+'))')
                # Author filters must not use the actor in the historical phrase.
                await query(user='STVOLove')
                assert await evaluate("rowCount()>0 && Array.from({length:rowCount()},(_,i)=>rowAt(i)).every(r=>r.user==='STVOLove' && r.notice.actor==='xSTVOLx')")
                await query(user='xSTVOLx'); assert await evaluate('rowCount()') == 0
                await query()
                # Native partial selection, then eviction, inert rich/plain copy.
                expected = await evaluate(r'''(()=>{const a=elList.querySelector('.row[data-id="0"] .notice-system span'),b=elList.querySelector('.row[data-id="5"] .notice-system span');getSelection().setBaseAndExtent(a.firstChild,3,b.firstChild,12);rememberSelection();const a0=selectionRange.anchor,f0=selectionRange.focus;return Array.from({length:6},(_,i)=>{let t=selectionText(rowAt(i));if(i===5)t=t.slice(0,f0.offset);if(i===0)t=t.slice(a0.offset);return t}).join('\n')})()''')
                picked = await evaluate('JSON.parse(JSON.stringify(selectionRange))')
                assert picked['anchor']['id'] == 0 and picked['focus']['id'] == 5
                await jump(count-100, width, count)
                assert await evaluate(r'!elList.querySelector(".row[data-id=\"0\"]")'), (width, count, mode)
                requests.clear()
                copied = await evaluate('''(()=>{const d=new DataTransfer();document.dispatchEvent(new ClipboardEvent('copy',{clipboardData:d,cancelable:true}));return {plain:d.getData('text/plain'),rich:d.getData('text/html')}})()''')
                await asyncio.sleep(.05); await evaluate('true')
                assert copied['plain'] == expected, (copied['plain'], expected)
                assert 'notice-system' in copied['rich'] and '<script>' not in copied['rich']
                assert not requests, 'Inert clipboard caused requests'
                # Visit mixed block boundaries and return, preserving bounded, ordered DOM.
                maximum = 0
                for index in (2400, count//2, count-1, 800, 0):
                    await jump(index, width, count)
                    state = await evaluate('''({n:elList.querySelectorAll('.row').length,ordered:[...elList.querySelectorAll('.row')].every((r,i,a)=>!i || +r.dataset.id===+a[i-1].dataset.id+1),present:visibleAnchor()!==null})''')
                    maximum = max(maximum, state['n'])
                    assert state['n'] <= 2400 and state['ordered'] and state['present'], state
                maximum = max(maximum, await evaluate('window.noticePeak||0'))
                assert maximum <= 2400
                assert await evaluate('selectionRange.anchor.id') == 0
                # Real scroll with notice height measurement, not just programmatic scrub.
                await evaluate('window.scrollBy(0,700);true'); await settled()
                assert await evaluate("visibleAnchor()!==null && elList.querySelectorAll('.row').length<=2400")
                if count == min(args.sizes) and mode == 'offline':
                    assert await evaluate("typeof html2canvas==='function'")
                    png = await evaluate('createSnapshotDataUrl(Array.from({length:12},(_,i)=>rowAt(i)),true,true)')
                    assert png.startswith('data:image/png;base64,')
                    (out/f'notices-export-{width}.png').write_bytes(base64.b64decode(png.split(',')[1]))
                    assert await evaluate("!document.querySelector('.export-wrap')")
                assert not errors, errors
                results.append(dict(width=width, count=count, mode=mode, max_dom=maximum, navigation='scrub+scroll' if width<600 else 'scroll', checks='render/search/author-filter/partial-selection/evicted-copy/scroll/XSS/overflow PASS'))
                (out/('notice-results-'+str(width)+'.json')).write_text(json.dumps(results, indent=2), encoding='utf-8')
