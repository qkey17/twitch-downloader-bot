// Exercise the actual overlay implementation without a browser or network.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const template = fs.readFileSync(path.join(__dirname, '../src/viewer_template.html'), 'utf8');
const code = template.slice(template.indexOf('let vodOverlay ='), template.indexOf('elVodCard.addEventListener("click", openVodOverlay)'));
function setup(meta) {
  function element(tag) {
    return {tag, children: [], events: {}, style: {}, isConnected: true,
      classList: {add() {}, remove() {}},
      appendChild(child) { this.children.push(child); },
      addEventListener(name, callback) { this.events[name] = callback; },
      remove() { this.isConnected = false; }};
  }
  const body = element('body'), images = [], opened = [];
  const context = vm.createContext({meta, document: {body, createElement: element},
    Image: function() { images.push(this); }, window: {open: (...args) => opened.push(args)}});
  vm.runInContext(code, context);
  return {context, body, images, opened};
}
const metadata = {thumbnail_full_url: 'https://synthetic.invalid/full', vod_url: 'https://synthetic.invalid/vod'};
test('button has exact label without emote, opens existing URL and overlay closes', () => {
  const {context, body, images, opened} = setup(metadata);
  context.openVodOverlay();
  const overlay = body.children[0], panel = overlay.children[0], button = panel.children[2].children[0];
  assert.equal(panel.className, 'snapshot-panel vod-preview-panel');
  assert.equal(button.textContent, '🔗 Ссылка на VOD'); assert.equal(button.children.length, 0);
  assert.equal(images[0].src, metadata.thumbnail_full_url);
  images[0].onload(); assert.equal(panel.children[0].src, metadata.thumbnail_full_url);
  button.events.click({stopPropagation() {}});
  assert.deepEqual(opened, [[metadata.vod_url, '_blank']]);
  assert.equal(body.style.overflow, 'hidden');
  overlay.events.click(); assert.equal(overlay.isConnected, false); assert.equal(body.style.overflow, '');
});
test('absent preview does not open overlay; absent VOD URL is not invented', () => {
  const empty = setup({}); empty.context.openVodOverlay(); assert.equal(empty.body.children.length, 0);
  const missingUrl = setup({thumbnail_preview_url: metadata.thumbnail_full_url});
  missingUrl.context.openVodOverlay();
  missingUrl.body.children[0].children[0].children[2].children[0].events.click({stopPropagation() {}});
  assert.deepEqual(missingUrl.opened, []);
});
test('image error and stale image callbacks preserve existing handling', () => {
  const {context, body, images} = setup(metadata);
  context.openVodOverlay(); const panel = body.children[0].children[0];
  images[0].onerror(); assert.equal(panel.children[1].textContent, 'Не удалось загрузить изображение');
  body.children[0].events.click();
  images[0].onload(); assert.equal(panel.children[0].src, undefined);
});
