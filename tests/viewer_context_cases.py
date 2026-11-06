"""Synthetic Chromium input and geometry checks for the chat context overlay."""

import asyncio
import json


def prepare_context_rows(rows):
    rows[:4] = [
        dict(t='00:00:00', user='日本語', login='KanaUser', text='Cafe\u0301 Kappa', fragments=[
            dict(text='Ca'), dict(text='fe\u0301 '), dict(text='Kappa', emoteID='25')],
            badges=[dict(set_id='subscriber', version='1')]),
        dict(t='00:00:01', user='Other', login='other', text='KanaUser CAFÉ'),
        dict(t='00:00:02', user='Unsafe', login='example/../../else', text='unsafe'),
        dict(t='00:00:03', user='Long', text='Long', fragments=[
            dict(text='<img onerror="window.fixtureXSS=1"> ' + 'A'*600, emoteID='25')]),
    ]


async def check_context(call, evaluate, wait, out, args, errors, requests):
    count = args.sizes[0]
    await call('DOM.enable')
    geometry = {}

    async def load(width, version='new', mode='online'):
        errors.clear()
        await call('Emulation.setDeviceMetricsOverride', dict(
            width=width, height=900, deviceScaleFactor=1, mobile=width < 900))
        await call('Emulation.setTouchEmulationEnabled', dict(enabled=width < 900, maxTouchPoints=2))
        await call('Page.navigate', dict(url=(out/f'{version}-{count}-{mode}.html').as_uri()))
        await wait("document.readyState==='complete' && !windowBusy && !FILTERING && !!document.querySelector('.row .emote')")

    async def pos(selector):
        return await evaluate('''(()=>{const r=document.querySelector(%s).getBoundingClientRect();
            return {x:r.left+r.width/2,y:r.top+r.height/2};})()''' % json.dumps(selector))

    async def mouse(event, selector=None):
        point = await pos(selector) if selector else dict(x=4, y=830)
        payload = dict(type=event, **point)
        if event != 'mouseMoved':
            payload.update(button='left', clickCount=1)
        await call('Input.dispatchMouseEvent', payload)

    async def click(selector):
        await mouse('mousePressed', selector)
        await mouse('mouseReleased', selector)

    async def touch(event, point=None):
        await call('Input.dispatchTouchEvent', dict(type=event, touchPoints=[] if point is None else [point]))

    async def tap(selector, seconds=0):
        await touch('touchStart', await pos(selector))
        if seconds:
            await asyncio.sleep(seconds)
        await touch('touchEnd')

    popup = "document.querySelector('#chatContext')"
    hidden = popup+'.hidden'
    emote = '.row[data-id="0"] .emote'
    badge = '.row[data-id="0"] .badge'
    user = '.row[data-id="0"] .user'
    long = '.row[data-id="3"] .emote'
    for width in (320, 390, 1440):
        for version in ('base', 'new'):
            await load(width, version)
            if width < 900:
                await evaluate('elPanelHandle.click()')
                await wait('!panelAnimating && PANEL_EXPANDED')
            geometry[width, version] = await evaluate('''[elQ,elU,elExportBtn,elToggleTime,
                ...(MOBILE_MQ.matches?[elScrubber,elPanelHandle]:[])].map(n=>n.getBoundingClientRect().height)''')
        assert geometry[width, 'base'] == geometry[width, 'new']
        assert len(set(geometry[width, 'new'])) == 1, geometry

    await load(1440)
    await wait("getComputedStyle(document.querySelector('.row')).display!=='none'")
    for selector in (emote, badge, user):
        target = await call('Runtime.evaluate', dict(expression=f'document.querySelector({json.dumps(selector)})'))
        listeners = await call('DOMDebugger.getEventListeners', dict(objectId=target['result']['objectId']))
        assert listeners['listeners'] == [], listeners
    initial_requests = len(requests)
    await mouse('mouseMoved', user)
    assert await evaluate(hidden)
    await mouse('mouseMoved', emote)
    await wait(popup+".textContent==='Kappa'")
    assert await evaluate(popup+'.offsetHeight===elQ.offsetHeight')
    await mouse('mouseMoved')
    await wait(hidden)
    await click(emote)
    await mouse('mouseMoved', badge)
    assert await evaluate(popup+".textContent==='Kappa' && !"+hidden)
    await click(badge)
    await wait(popup+".textContent==='Subscriber'")
    await click(user)
    await wait(popup+".querySelector('a')?.href==='https://www.twitch.tv/kanauser'")
    assert await evaluate("!document.querySelector('.row[data-id=\"2\"] .user').hasAttribute('data-chat-context')")
    await evaluate('''document.addEventListener('click',e=>{const a=e.target.closest('#chatContext a');
        if(a){window.profileClick=[a.href,e.defaultPrevented,a.rel];e.preventDefault();}},{once:true})''')
    await click('#chatContext a')
    assert await evaluate('profileClick') == ['https://www.twitch.tv/kanauser', False, 'noopener noreferrer']
    assert len(requests) == initial_requests
    await evaluate(f'document.querySelector({json.dumps(user)}).focus()')
    await call('Input.dispatchKeyEvent', dict(type='keyDown', key='Enter'))
    await wait("document.activeElement===document.querySelector('#chatContext a')")
    await call('Input.dispatchKeyEvent', dict(type='keyDown', key='Escape'))
    assert await evaluate(f"{hidden} && document.activeElement===document.querySelector({json.dumps(user)})")
    await click(badge)
    await click('#q')
    await wait(hidden)
    await click(badge)
    await call('Emulation.setDeviceMetricsOverride',dict(width=1360,height=870,deviceScaleFactor=1,mobile=False))
    await wait(hidden)
    await evaluate('elQ.value="CAFÉ";elU.value="NAUSER";applyFilters()')
    assert await evaluate("rowCount()===1 && elStat.textContent.includes('1 / ')")
    assert await evaluate("Array.from(elList.querySelectorAll('.msg mark'),n=>n.textContent).join('')==='Cafe\\u0301'")
    assert await evaluate("elList.querySelector('.user mark').textContent==='naUser'")
    assert await evaluate('''(()=>{const selection=getSelection();
        selection.selectAllChildren(elList.querySelector('.msg'));
        const text=selection.toString();selection.removeAllRanges();
        return text==='Cafe\\u0301 ';})()''')
    await evaluate('elU.value="カナ";applyFilters()')
    assert await evaluate('rowCount()===0')
    await evaluate('elU.value="日本";applyFilters()')
    assert await evaluate("rowCount()===1 && elList.querySelector('.user mark').textContent==='日本'")
    assert not errors, errors

    for width, mode in ((320, 'online'), (390, 'offline')):
        await load(width, mode=mode)
        await tap(emote)
        await wait(popup+".textContent==='Kappa'")
        await tap(badge)
        await wait(popup+".textContent==='Subscriber'")
        await tap(user)
        await wait(popup+".querySelector('a')!==null")
        await touch('touchStart', dict(x=8,y=820))
        await touch('touchEnd')
        await wait(hidden)
        await tap(emote, .65)
        await asyncio.sleep(.15)
        assert await evaluate(hidden)
        # Test compatibility click suppression separately from the emulated
        # press. The listener leaves the native contextmenu default intact.
        assert await evaluate('''(()=>{const n=document.querySelector('.row[data-id="0"] .emote');
          const menu=new MouseEvent('contextmenu',{bubbles:true,cancelable:true});
          const allowed=n.dispatchEvent(menu);
          n.dispatchEvent(new MouseEvent('click',{bubbles:true,detail:1}));
          return allowed && !menu.defaultPrevented && chatContext.hidden;})()''')
        point=await pos(emote)
        await touch('touchStart', point)
        await touch('touchMove', dict(x=point['x'],y=point['y']+35))
        await touch('touchEnd')
        assert await evaluate(hidden)
        await evaluate('scrollTo(0,0)')
        await wait('scrollY===0 && !windowBusy')
        await tap(long)
        await wait("!chatContext.hidden")
        assert await evaluate('''(()=>{const r=chatContext.getBoundingClientRect();return r.left>=0
            && r.right<=innerWidth && r.top>=viewportInset() && r.bottom<=innerHeight
            && r.height>46 && chatContext.children.length===0 && window.fixtureXSS===undefined;})()''')
        await call('Emulation.setPageScaleFactor',dict(pageScaleFactor=1.25))
        await wait(hidden)
        await call('Emulation.setPageScaleFactor',dict(pageScaleFactor=1))
        await tap(emote)
        await wait("!chatContext.hidden")
        await evaluate('scrollBy(0,160)')
        await wait(hidden)
        assert await evaluate("document.querySelectorAll('#chatContext').length===1 && document.querySelectorAll('#list .row').length<=2400")
        assert not errors, errors
    print(json.dumps(dict(control_heights={w: geometry[w,'new'] for w in (320,390,1440)},
                          search='PASS', desktop='PASS', mobile='PASS'), ensure_ascii=False))
