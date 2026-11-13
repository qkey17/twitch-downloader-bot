"""Synthetic Chromium input and geometry checks for the chat context overlay."""

import asyncio
import json


def prepare_context_rows(rows):
    mention_tail = ('mail@KanaUser https://example.org/@KanaUser @KanaUserSuffix @Unknown '
                    'Ж@KanaUser @KanaUser界 <b> (@情報本部), @ijuos @共有名')
    links = [dict(text=part) for part in ('(clips.twitch.', 'tv/', 'Kappa', '?to=@情報本部), @情報本部 ')]
    links.append(dict(text='Kappa', emoteID='25'))
    rows[:9] = [
        dict(t='00:00:00', user='日本語', login='KanaUser', color='#12abef', text='Cafe\u0301 Kappa', fragments=[
            dict(text='Ca'), dict(text='fe\u0301 '), dict(text='Kappa', emoteID='25')],
            badges=[dict(set_id='subscriber', version='1')]),
        dict(t='00:00:01', user='Other', login='other', text='KanaUser CAFÉ'),
        dict(t='00:00:02', user='Unsafe', login='example/../../else', text='unsafe'),
        dict(t='00:00:03', user='Long', text='Long', fragments=[
            dict(text='<img onerror="window.fixtureXSS=1"> ' + 'A'*600, emoteID='26')]),
        dict(t='00:00:04', user='Mentions', login='mentions',
            text='Hi (@KANAuser), @FixtureStreamer @FarAway, '+mention_tail,
            fragments=[dict(text='Hi (@KA'), dict(text='NAuser), @FixtureStreamer @FarAway, '),
                       dict(text=mention_tail)],
            badges=[dict(set_id=name, version='1') for name in ('event','lead_moderator','unknown')]),
        dict(t='00:00:05', user='情報本部', login='ijuos', color='#317acb', text='test'),
        dict(t='00:00:06', user='共有名', login='first', text='first'),
        dict(t='00:00:07', user='共有名', login='second', text='second'),
        dict(t='00:00:08', user='Links', login='links', text=''.join(f['text'] for f in links), fragments=links),
    ]
    rows[-1].update(user='Far Away', login='FarAway', color='#fb73a2')


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
        if version == 'new':
            await wait("mentionUsers.has('faraway') && !windowBusy && !!document.querySelector('.chat-mention')")

    async def pos(selector):
        return await evaluate('''(()=>{const r=document.querySelector(%s).getClientRects()[0];
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

    async def check_link(touchscreen=False):
        await evaluate('elQ.value="kappa?to=@情報";elU.value="";applyFilters()')
        selector = '#list .msg .chat-link'
        assert await evaluate('''(()=>{
          const msg=elList.querySelector('.msg'),link=msg.querySelector('.chat-link');
          const expected='(clips.twitch.tv/Kappa?to=@情報本部), @情報本部 ';
          const selected=getSelection();selected.selectAllChildren(msg);
          const ok=rowCount()===1 && msg.textContent===expected && selected.toString()===expected
            && selectionText(rowAt(0)).endsWith(expected)
            && link.textContent==='clips.twitch.tv/Kappa?to=@情報本部'
            && link.href==='https://clips.twitch.tv/Kappa?to=@%E6%83%85%E5%A0%B1%E6%9C%AC%E9%83%A8'
            && link.target==='_blank' && link.rel==='noopener noreferrer'
            && link.querySelector('mark')?.textContent==='Kappa?to=@情報'
            && msg.querySelectorAll('.chat-mention').length===1 && !link.querySelector('.chat-mention')
            && msg.querySelectorAll('.emote').length===1;
          selected.removeAllRanges();return ok;
        })()''')
        await evaluate('window.linkClicks=[];window.open=(...args)=>linkClicks.push(args);true')
        if touchscreen:
            await tap(selector)
        else:
            await click(selector)
        await wait("elList.querySelector('.is-message-pinned')?.dataset.id==='0'")
        await wait('linkConfirm.open')
        assert await evaluate("""linkClicks.length===0 && chatContext.hidden
          && linkDestination.textContent===elList.querySelector('.chat-link').href
          && !linkDestination.querySelector('a') && getComputedStyle(document.documentElement).overflow==='hidden'""")
        await call('Input.dispatchKeyEvent',dict(type='keyDown',key='Escape',windowsVirtualKeyCode=27))
        await wait("!linkConfirm.open && !document.documentElement.classList.contains('confirming-link')")
        assert await evaluate("linkClicks.length===0 && document.activeElement===elList.querySelector('.chat-link')")
        await wait("!!elList.querySelector('.clip-card')")
        assert await evaluate("""elList.querySelector('.clip-card strong').textContent==='<img onerror=window.fixtureXSS=1>'
          && elList.querySelector('.clip-card small').textContent==='Clip Author'
          && elList.querySelectorAll('.clip-card img').length===1 && window.fixtureXSS===undefined""")
        await (tap if touchscreen else click)('.clip-card')
        await wait('linkConfirm.open')
        await mouse('mousePressed')
        await mouse('mouseReleased')
        await wait("!linkConfirm.open && !document.documentElement.classList.contains('confirming-link')")
        assert await evaluate('linkClicks.length===0')
        await evaluate("elList.querySelector('.chat-link').focus();true")
        await call('Input.dispatchKeyEvent',dict(type='keyDown',key='Enter',windowsVirtualKeyCode=13))
        await wait('linkConfirm.open')
        if touchscreen:
            await tap('#linkConfirm button[value="open"]')
        else:
            await click('#linkConfirm button[value="open"]')
        await wait('!linkConfirm.open')
        assert await evaluate("linkClicks.length===1 && linkClicks[0][0]===elList.querySelector('.chat-link').href && linkClicks[0][1]==='_blank' && linkClicks[0][2]==='noopener,noreferrer'")
        assert await evaluate('''elList.querySelector('.chat-link').dispatchEvent(
          new MouseEvent('contextmenu',{bubbles:true,cancelable:true}))''')
        if not touchscreen:
            assert await evaluate('''(async()=>{
              const data=[rowAt(0)], wrap=await buildSnapshotWrap(data,true,false);
              return wrap.querySelector('.chat-link')?.textContent==='clips.twitch.tv/Kappa?to=@情報本部'
                && (await createSnapshotDataUrl(data,true,false)).startsWith('data:image/png;base64,');
            })()''')

    popup = "document.querySelector('#chatContext')"
    hidden = popup+'.hidden'
    emote = '.row[data-id="0"] .emote'
    badge = '.row[data-id="0"] .badge'
    user = '.row[data-id="0"] .user'
    long = '.row[data-id="3"] .emote'
    mention = '.row[data-id="4"] .chat-mention'
    global_emote = "chatContext.querySelector('.chat-context-emote')?.firstChild.textContent==='Kappa' && " \
                   "chatContext.querySelector('.chat-context-source')?.textContent==='Global Emotes'"
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
    for selector in (emote, badge, user, mention):
        target = await call('Runtime.evaluate', dict(expression=f'document.querySelector({json.dumps(selector)})'))
        listeners = await call('DOMDebugger.getEventListeners', dict(objectId=target['result']['objectId']))
        assert listeners['listeners'] == [], listeners
    initial_requests = len(requests)
    await mouse('mouseMoved', user)
    assert await evaluate(hidden)
    await mouse('mouseMoved', emote)
    await wait(global_emote)
    assert await evaluate(popup+'.offsetHeight>=elQ.offsetHeight')
    await mouse('mouseMoved')
    await wait(hidden)
    await click(emote)
    await mouse('mouseMoved', badge)
    assert await evaluate(global_emote+" && !"+hidden)
    await click(badge)
    await wait(popup+".textContent==='Subscriber'")
    await click(user)
    await wait(popup+".querySelector('a')?.href==='https://www.twitch.tv/kanauser'")
    styles = await evaluate('''(()=>{
      const profile=getComputedStyle(chatContext), control=getComputedStyle(elExportBtn);
      return ['backgroundColor','borderColor','borderRadius','color','height','fontFamily','fontSize']
        .filter(k=>profile[k]!==control[k]).map(k=>[k,profile[k],control[k]]);
    })()''')
    assert not styles, styles
    assert await evaluate("getComputedStyle(chatContext.firstChild).textDecorationLine==='none'")
    await call('CSS.enable')
    root = (await call('DOM.getDocument'))['root']['nodeId']
    profile_node = (await call('DOM.querySelector', dict(nodeId=root, selector='#chatContext')))['nodeId']
    control_node = (await call('DOM.querySelector', dict(nodeId=root, selector='#toggleTime')))['nodeId']
    for state in ('hover', 'active'):
        for node in (profile_node, control_node):
            await call('CSS.forcePseudoState', dict(nodeId=node, forcedPseudoClasses=[state]))
        await wait("""(()=>{
          const profile=getComputedStyle(chatContext), control=getComputedStyle(elToggleTime);
          return profile.boxShadow!=='none'
            && ['backgroundColor','borderColor','boxShadow','borderRadius','color'].every(k=>profile[k]===control[k])
            && (%s || profile.transform==='none' && control.transform!=='none');
        })()""" % ('true' if state == 'active' else 'false'))
    for node in (profile_node, control_node):
        await call('CSS.forcePseudoState', dict(nodeId=node, forcedPseudoClasses=[]))
    await wait("getComputedStyle(chatContext).transform==='none'")
    assert await evaluate("!document.querySelector('.row[data-id=\"2\"] .user').hasAttribute('data-chat-context')")
    await evaluate('''window.profileClicks=[];document.addEventListener('click',e=>{
        const a=e.target.closest('#chatContext a');
        if(a){profileClicks.push([a.href,e.defaultPrevented,a.rel]);e.preventDefault();}
    })''')
    await click('#chatContext a')
    padding = await evaluate('''(()=>{const r=chatContext.getBoundingClientRect();
        const range=document.createRange();range.selectNodeContents(chatContext.firstChild);
        const text=range.getBoundingClientRect();
        const x=r.left+4,y=r.top+r.height/2;
        return r.height===46 && x<text.left ? {x,y} : null;})()''')
    assert padding
    for event in ('mousePressed', 'mouseReleased'):
        await call('Input.dispatchMouseEvent', dict(type=event, button='left', clickCount=1, **padding))
    assert await evaluate('profileClicks') == [['https://www.twitch.tv/kanauser', False, 'noopener noreferrer']] * 2
    assert len(requests) == initial_requests
    await evaluate(f'document.querySelector({json.dumps(user)}).focus()')
    await call('Input.dispatchKeyEvent', dict(type='keyDown', key='Enter'))
    await wait("document.activeElement===document.querySelector('#chatContext a')")
    assert await evaluate("""chatContext.firstChild.matches(':focus-visible')
        && getComputedStyle(chatContext).outlineStyle==='solid'
        && getComputedStyle(chatContext).outlineWidth==='2px'""")
    await call('Input.dispatchKeyEvent', dict(type='keyDown', key='Escape'))
    assert await evaluate(f"{hidden} && document.activeElement===document.querySelector({json.dumps(user)})")
    await click(badge)
    await click('#q')
    await wait(hidden)
    await click(badge)
    await call('Emulation.setDeviceMetricsOverride',dict(width=1360,height=870,deviceScaleFactor=1,mobile=False))
    await wait(hidden)
    assert await evaluate('''(()=>{
      const matches=[...document.querySelectorAll('.row[data-id="4"] .chat-mention')];
      const author=document.querySelector('.row[data-id="5"] .user');
      return matches.map(n=>n.textContent).join(',')==='@KANAuser,@FixtureStreamer,@FarAway,@情報本部,@ijuos'
        && matches[0].style.color===document.querySelector('.row[data-id="0"] .user').style.color
        && matches[1].style.color==='rgb(255, 255, 255)'
        && matches.every(node=>getComputedStyle(node).fontWeight==='700')
        && getComputedStyle(matches[0].parentElement).fontWeight==='400'
        && matches[2].style.color==='rgb(251, 115, 162)'
        && matches.slice(3).every(n=>n.style.color===author.style.color && n.dataset.chatContext==='ijuos')
        && !document.querySelector(`.row[data-id="${rows.length-1}"]`)
        && !document.querySelector('.row[data-id="4"] .msg b');
    })()''')
    await mouse('mouseMoved', mention)
    assert await evaluate(hidden)
    await click(mention)
    await wait("chatContext.firstChild?.href==='https://www.twitch.tv/kanauser'")
    assert await evaluate('''(()=>{
      const target=document.querySelector('.chat-mention').getBoundingClientRect(),r=chatContext.getBoundingClientRect();
      return Math.abs(target.top-r.bottom-6)<1 && Math.abs((target.left+target.right-r.left-r.right)/2)<1;
    })()''')
    for suffix in ('', ':last-of-type'):
        await call('Input.dispatchKeyEvent', dict(type='keyDown', key='Escape'))
        await click('.row[data-id="4"] .chat-mention[data-chat-context="ijuos"]'+suffix)
        await wait("chatContext.firstChild?.href==='https://www.twitch.tv/ijuos'")
    for i, title in enumerate(('Event <Final>', 'Lead Moderator')):
        await click(f'.row[data-id="4"] .badge:nth-child({i+1})')
        await wait(f"chatContext.textContent==={json.dumps(title)} && chatContext.children.length===0")
    await click('.row[data-id="4"] .badge:nth-child(3)')
    await wait(hidden)
    await evaluate('elQ.value="@情報本部), @IJU";applyFilters()')
    assert await evaluate('''(()=>{
      const msg=elList.querySelector('.msg'), selection=getSelection();
      selection.selectAllChildren(msg);rememberSelection();
      const event=new ClipboardEvent('copy',{clipboardData:new DataTransfer(),cancelable:true,bubbles:true});
      msg.dispatchEvent(event);
      const expected=messageText(rows[4]);
      const ok=rowCount()===1 && msg.textContent===expected && selection.toString()===expected
        && !event.defaultPrevented && selectionText(decodeMessage(rows[4])).endsWith(expected)
        && msg.querySelector('.chat-mention mark')?.textContent==='@情報本部'
        && [...msg.querySelectorAll('mark')].map(n=>n.textContent).join('')==='@情報本部), @iju';
      selection.removeAllRanges();return ok;
    })()''')
    assert await evaluate("typeof html2canvas==='function'"), 'Provide cached html2canvas.min.js in output directory'
    assert await evaluate('''(async()=>{
      const data=[decodeMessage(rows[4])], wrap=await buildSnapshotWrap(data,true,false);
      return wrap.querySelector('.msg').textContent===data[0].text
        && wrap.querySelectorAll('.chat-mention').length===5
        && (await createSnapshotDataUrl(data,true,false)).startsWith('data:image/png;base64,');
    })()''')
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
    await check_link()
    assert not errors, errors

    for width, mode in ((320, 'online'), (390, 'offline')):
        await load(width, mode=mode)
        await tap(emote)
        await wait(global_emote)
        assert await evaluate('chatContext.getBoundingClientRect().top > contextAnchor.getBoundingClientRect().bottom')
        await tap(badge)
        await wait(popup+".textContent==='Subscriber'")
        await tap(user)
        await wait(popup+".querySelector('a')!==null")
        await touch('touchStart', dict(x=8,y=820))
        await touch('touchEnd')
        await wait(hidden)
        await tap(mention, .65)
        assert await evaluate(hidden)
        await tap(mention)
        await wait("chatContext.firstChild?.href==='https://www.twitch.tv/kanauser'")
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
            && r.height>46 && chatContext.querySelector('.chat-context-source')?.textContent==='Other Emotes'
            && window.fixtureXSS===undefined;})()''')
        await call('Emulation.setPageScaleFactor',dict(pageScaleFactor=1.25))
        await wait(hidden)
        await call('Emulation.setPageScaleFactor',dict(pageScaleFactor=1))
        await tap(emote)
        await wait("!chatContext.hidden")
        await evaluate('scrollBy(0,160)')
        await wait(hidden)
        assert await evaluate("document.querySelectorAll('#chatContext').length===1 && document.querySelectorAll('#list .row').length<=2400")
        await check_link(touchscreen=True)
        assert not errors, errors

    async def check_row_activation(width, touchscreen):
        await load(width, mode='offline')
        await call('Emulation.setTouchEmulationEnabled', dict(enabled=touchscreen, maxTouchPoints=1))
        await evaluate('''window.pinEvents=[];
          const originalPin=pinMessage;
          pinMessage=(row,...args)=>{pinEvents.push(+row.dataset.id);originalPin(row,...args);};
          document.addEventListener('click',e=>{if(e.target.closest('.chat-link'))e.preventDefault();}, {capture:true});
          true''')
        for index, selector, context in (
            (0, user, 'profile'), (4, mention, 'profile'), (1, '.row[data-id="1"] .msg', None),
            (2, '.row[data-id="2"]', None), (0, badge, 'Subscriber'),
            (0, emote, 'Kappa'), (8, '.row[data-id="8"] .chat-link', None),
        ):
            point=await pos(selector)
            if selector.endswith('"]'):
                point['x']=await evaluate(f'document.querySelector({json.dumps(selector)}).getBoundingClientRect().right-4')
            before=await evaluate('pinEvents.length')
            if touchscreen:
                await touch('touchStart', point)
                await touch('touchEnd')
            else:
                await call('Input.dispatchMouseEvent', dict(type='mouseMoved', **point))
                for event in ('mousePressed', 'mouseReleased'):
                    await call('Input.dispatchMouseEvent', dict(type=event, button='left', clickCount=1, **point))
            await wait(f'elList.querySelector(".is-message-pinned")?.dataset.id==="{index}"')
            if selector.endswith('.chat-link'):
                await wait('linkConfirm.open')
                await click('#linkConfirm button[value="cancel"]')
                await wait('!linkConfirm.open')
            assert await evaluate(f'pinEvents.length==={before+1} && pinEvents.at(-1)==={index}')
            if context:
                if context=='profile':
                    await wait("!chatContext.hidden && chatContext.firstChild?.href==='https://www.twitch.tv/kanauser'")
                    await wait('!!contextAnchor?.classList.contains("is-nickname-highlighted")')
                else:
                    await wait(f'!chatContext.hidden && chatContext.textContent.includes({json.dumps(context)})')
                await call('Input.dispatchKeyEvent',dict(type='keyDown',key='Escape'))
                await wait(hidden)
                if touchscreen and context=='profile':
                    await wait('!elList.querySelector(".is-message-pinned,.is-nickname-highlighted")')
                else:
                    assert await evaluate(f'elList.querySelector(".is-message-pinned")?.dataset.id==="{index}"')
            if not touchscreen:
                await mouse('mouseMoved')
                await wait('!elList.querySelector(".is-nickname-highlighted")')
        # Native selection and compatibility clicks must not create a new pin.
        before=await evaluate('pinEvents.length')
        await evaluate("""const row=elList.querySelector('.row[data-id="1"]');
          row.dispatchEvent(new MouseEvent('click',{bubbles:true,detail:1}));true""")
        point=await pos('.row[data-id="1"] .msg')
        if touchscreen:
            await touch('touchStart',point)
            await touch('touchMove',dict(x=point['x'],y=point['y']-42))
            await touch('touchEnd')
        else:
            await call('Input.dispatchMouseEvent',dict(type='mousePressed',button='left',buttons=1,clickCount=1,**point))
            assert await evaluate("getSelection().selectAllChildren(row.querySelector('.msg'));!!getSelection().toString()")
            await call('Input.dispatchMouseEvent',dict(type='mouseReleased',button='left',buttons=0,clickCount=1,**point))
            assert await evaluate(f'pinEvents.length==={before}')
            await evaluate('getSelection().removeAllRanges();true')
            await call('Input.dispatchMouseEvent',dict(type='mousePressed',button='left',buttons=1,clickCount=1,**point))
            end=dict(x=point['x']+34,y=point['y'])
            await call('Input.dispatchMouseEvent',dict(type='mouseMoved',buttons=1,**end))
            await call('Input.dispatchMouseEvent',dict(type='mouseReleased',button='left',buttons=0,clickCount=1,**end))
        assert await evaluate(f'pinEvents.length==={before}')
        assert not errors, errors

    for width in (900,901):
        for touchscreen in (False,True):
            await check_row_activation(width,touchscreen)

    await load(320,mode='offline')
    await call('Emulation.setTouchEmulationEnabled',dict(enabled=False))
    await evaluate("""const row=elList.querySelector('.row[data-id="4"]');
      Object.assign(row.querySelector('.msg').style,{display:'inline-block',width:'48px'});true""")
    await wait(f'document.querySelector({json.dumps(mention)}).getClientRects().length>1')
    height=await evaluate('row.offsetHeight')
    await mouse('mouseMoved',mention)
    await wait(f'''(()=>{{
      const name=document.querySelector({json.dumps(mention)}), style=getComputedStyle(name);
      return row.classList.contains('is-message-hovered') && name.classList.contains('is-nickname-highlighted')
        && !name.classList.contains('has-ink-highlight') && style.boxDecorationBreak==='clone'
        && style.fontWeight==='700' && style.color==='rgb(18, 171, 239)' && row.offsetHeight==={height};
    }})()''')
    await load(390,mode='offline')
    unknown=await evaluate("""(()=>{
      const message=elList.querySelector('.row[data-id="4"] .msg');
      const walker=document.createTreeWalker(message,NodeFilter.SHOW_TEXT);
      while(walker.nextNode()){
        const node=walker.currentNode,start=node.data.indexOf('@Unknown');
        if(start<0) continue;
        const range=document.createRange();range.setStart(node,start);range.setEnd(node,start+8);
        const r=range.getClientRects()[0];return {x:r.left+r.width/2,y:r.top+r.height/2};
      }
    })()""")
    await touch('touchStart',unknown)
    await touch('touchEnd')
    await wait('elList.querySelector(".is-message-pinned")?.dataset.id==="4" && chatContext.hidden && !elList.querySelector(".is-nickname-highlighted")')

    for width in (320,900,901,1440):
        await load(width,mode='offline')
        await call('Emulation.setPageScaleFactor',dict(pageScaleFactor=1.25))
        await evaluate('scrollTo(0,elList.offsetTop+heightBefore(Math.floor(blockCount()/2)));true')
        await wait('!windowBusy && +desktopAutomaticRow(chatViewport())?.dataset.id>rowCount()/3')
        frame=await evaluate("""(()=>{
          const style=getComputedStyle(elList), bounds=chatViewport();
          const full=[...elList.querySelectorAll('.row')].filter(row=>{
            const rect=row.getBoundingClientRect();return rect.top>=bounds.top-.5 && rect.bottom<=bounds.bottom+.5;
          });
          const before=[elList.clientWidth,elList.offsetHeight,scrollY];
          const outline=[style.outlineStyle,style.outlineColor,style.outlineWidth,style.outlineOffset,style.borderRadius,style.boxShadow];
          elList.style.outline='none';
          elList.style.boxShadow='none';
          const after=[elList.clientWidth,elList.offsetHeight,scrollY];
          elList.style.removeProperty('outline');
          elList.style.removeProperty('box-shadow');
          return {outline,before,after,scrollWidth:document.documentElement.scrollWidth,innerWidth,
            rows:elList.querySelectorAll('.row').length,full:full.length,
            automatic:desktopAutomaticRow(bounds)?.dataset.id,expected:full[0]?.dataset.id};
        })()""")
        if width<=900:
            assert [frame['outline'][0],*frame['outline'][4:]]==['none','0px',
                'rgb(48, 48, 54) 1px 0px 0px 0px inset, rgb(48, 48, 54) -1px 0px 0px 0px inset'], (width,frame)
        else:
            assert frame['outline']==['solid','rgb(48, 48, 54)','1px','-1px','14px','none'], (width,frame)
        assert frame['before']==frame['after'] and frame['scrollWidth']<=frame['innerWidth'], (width,frame)
        assert frame['rows']<=2400 and frame['full']>1 and frame['automatic']==frame['expected'], (width,frame)
        await call('Emulation.setPageScaleFactor',dict(pageScaleFactor=1))
        assert not errors, errors
    for scale in (1,1.25):
        await load(1440,mode='offline')
        await call('Emulation.setPageScaleFactor',dict(pageScaleFactor=scale))
        await evaluate("elQ.value='unique-last';applyFilters()")
        await wait('rowCount()===1 && !FILTERING && !windowBusy')
        gaps=await evaluate("""(()=>{
          const list=elList.getBoundingClientRect(), row=elList.querySelector('.row').getBoundingClientRect();
          return [row.top-list.top,list.bottom-row.bottom];
        })()""")
        assert abs(gaps[0]-gaps[1])<.5,(scale,gaps)
    await call('Emulation.setPageScaleFactor',dict(pageScaleFactor=1))
    for state in ('waiting','empty','broken'):
        await call('Page.navigate', dict(url=(out/f'{state}-{count}-offline.html').as_uri()))
        await wait("document.readyState==='complete' && !!document.getElementById('viewerLoading')")
        if state=='waiting':
            assert await evaluate("""viewerLoading.textContent==='Загрузка чата...'
              && getComputedStyle(document.querySelector('.page')).visibility==='hidden'""")
        elif state=='empty':
            await wait('viewerLoading.hidden && rowCount()===0')
        else:
            await wait("viewerLoading.textContent==='Не удалось загрузить чат.' && !viewerLoading.classList.contains('is-loading')")
    print(json.dumps(dict(control_heights={w: geometry[w,'new'] for w in (320,390,1440)},
                          search='PASS', mentions='PASS', copy='PASS', png='PASS',
                          desktop='PASS', mobile='PASS', links='PASS', row_activation='PASS',
                          chat_frame='PASS', rows=count), ensure_ascii=False))
