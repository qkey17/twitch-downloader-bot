// Exercise the actual overlay implementation without a browser or network.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const template = fs.readFileSync(path.join(__dirname, '../src/viewer_template.html'), 'utf8');
const code = template.slice(template.indexOf('let vodOverlay ='), template.indexOf('requestAnimationFrame(fitVodTitle);', template.indexOf('let vodOverlay =')));
function setup(meta) {
  const created = [];
  const document = {};
  function element(tag) {
    const result = {tag, children: [], events: {}, style: {}, isConnected: true,
      classList: {add() {}, remove() {}},
      focus() { document.activeElement = this; },
      setAttribute(name, value) { this[name] = value; },
      appendChild(child) { this.children.push(child); },
      addEventListener(name, callback) { this.events[name] = callback; },
      remove() { this.isConnected = false; }};
    created.push(result); return result;
  }
  const body = element('body'), images = [], opened = [];
  const card = element('div'), thumbnail = element('img');
  Object.assign(document, {body, createElement: element});
  const context = vm.createContext({meta, document, URL, elVodCard: card, elVodPreviewImg: thumbnail,
    Image: function() { images.push(this); }, window: {open: (...args) => opened.push(args)}});
  vm.runInContext(code, context);
  return {context, body, images, opened, document, card, thumbnail, created};
}
const metadata = {thumbnail_full_url: 'https://synthetic.invalid/full', vod_url: 'https://www.twitch.tv/videos/123456789'};
test('button has exact label without emote, opens existing URL and overlay closes', () => {
  const {context, body, images, opened} = setup(metadata);
  context.openVodOverlay();
  const overlay = body.children[0], panel = overlay.children[0], button = panel.children[2].children[0];
  assert.equal(panel.className, 'snapshot-panel vod-preview-panel');
  assert.equal(button.textContent, '🔗 Ссылка на VOD'); assert.equal(button.children.length, 0);
  assert.equal(images[0].src, metadata.thumbnail_full_url);
  images[0].onload(); assert.equal(panel.children[0].src, metadata.thumbnail_full_url);
  button.events.click({stopPropagation() {}});
  assert.deepEqual(opened, [[metadata.vod_url, '_blank', 'noopener,noreferrer']]);
  assert.equal(body.style.overflow, 'hidden');
  overlay.events.click(); assert.equal(overlay.isConnected, false); assert.equal(body.style.overflow, '');
});
test('absent preview does not open overlay; absent VOD URL is not invented', () => {
  const empty = setup({}); empty.context.openVodOverlay(); assert.equal(empty.body.children.length, 0);
  const missingUrl = setup({thumbnail_preview_url: metadata.thumbnail_full_url});
  missingUrl.context.openVodOverlay();
  assert.equal(missingUrl.body.children[0].children[0].children[2].children.length, 0);
  assert.deepEqual(missingUrl.opened, []);
});

test('LIVE opens the shared dialog without any image even with stale thumbnails', () => {
  for(const thumbnail_full_url of [undefined, metadata.thumbnail_full_url]){
    const s = setup({...metadata, thumbnail_full_url, captured_during_live:true});
    s.card.events.click();
    const overlay=s.body.children[0], panel=overlay.children[0];
    assert.match(panel.className, /vod-live-panel/);
    assert.equal(panel.role, 'dialog'); assert.equal(panel['aria-modal'], 'true');
    assert.equal(s.images.length, 0);
    assert.equal(s.created.filter(x=>x.tag==='img').length, 1); // Only original page placeholder.
    assert.equal(s.thumbnail.src, undefined);
    const button=panel.children[0].children[0];
    assert.equal(button.textContent, 'Открыть VOD');
    assert.equal(s.document.activeElement, button);
    button.events.click({stopPropagation(){}});
    assert.deepEqual(s.opened, [[metadata.vod_url,'_blank','noopener,noreferrer']]);
  }
});

test('LIVE mouse/backdrop, close button, Escape, keyboard activation and focus cycle', () => {
  for(const closing of ['backdrop','button','escape']){
    const s=setup({...metadata,captured_during_live:true});
    let prevented=0;
    s.card.events.keydown({key:'Enter',preventDefault(){prevented++;}});
    const overlay=s.body.children[0], panel=overlay.children[0];
    const link=panel.children[0].children[0], close=panel.children[1];
    overlay.events.keydown({key:'Tab',preventDefault(){prevented++;}});
    assert.equal(s.document.activeElement,close);
    overlay.events.keydown({key:'Tab',shiftKey:true,preventDefault(){}});
    assert.equal(s.document.activeElement,link);
    if(closing==='backdrop') overlay.events.click();
    if(closing==='button') close.events.click();
    if(closing==='escape') overlay.events.keydown({key:'Escape',preventDefault(){}});
    assert.equal(overlay.isConnected,false);
    assert.equal(s.body.style.overflow,'');
    assert.equal(s.document.activeElement,s.card);
    assert.ok(prevented);
    s.card.events.keydown({key:' ',preventDefault(){}});
    assert.equal(s.body.children.length,2);
  }
});

test('missing, malicious and non-Twitch LIVE links cannot open a dialog or request images', () => {
  for(const vod_url of [undefined,'', 'javascript:alert(1)', 'data:text/html,<script>x</script>',
    'https://www.twitch.tv.evil.invalid/videos/123', 'https://evil.invalid/videos/123',
    'https://user:pass@www.twitch.tv/videos/123','http://www.twitch.tv/videos/123',
    '//www.twitch.tv/videos/123', 'https://www.twitch.tv/videos/<img>', {}]){
    const s=setup({...metadata,vod_url,captured_during_live:true});
    s.card.events.click();
    assert.equal(s.body.children.length,0);
    assert.equal(s.card['aria-disabled'],'true');
    assert.equal(s.card.tabIndex,-1);
    assert.equal(s.images.length,0); assert.equal(s.thumbnail.src,undefined);
    assert.deepEqual(s.opened,[]);
  }
});
test('image error and stale image callbacks preserve existing handling', () => {
  const {context, body, images} = setup(metadata);
  context.openVodOverlay(); const panel = body.children[0].children[0];
  images[0].onerror(); assert.equal(panel.children[1].textContent, 'Не удалось загрузить изображение');
  body.children[0].events.click();
  images[0].onload(); assert.equal(panel.children[0].src, undefined);
});
