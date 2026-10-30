// Execute the real template functions with a minimal DOM double; no network/browser.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../src/viewer_template.html'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];

function render(text, local = {}, cdn = {}, remote = new Map(), fragment = null) {
  const element = tag => ({tag, children: [], appendChild(node) { this.children.push(node); }});
  const context = vm.createContext({LOCAL_EMOTES: local, CDN_EMOTES: cdn,
    document: {createElement: element, createTextNode: text => ({tag: '#text', text})}});
  const start = script.includes('function catalogEmoteUrl(')
    ? script.indexOf('function catalogEmoteUrl(') : script.indexOf('function renderTextWithEmotes(');
  vm.runInContext(script.slice(start, script.indexOf('const CHUNK =')), context);
  const result = element('span');
  if (fragment) context.renderFragmentsWithEmotes(result, [fragment], remote);
  else context.renderTextWithEmotes(result, text, remote);
  return result.children;
}
const paths = text => [null, {text}];
function assertText(nodes, text) {
  assert.ok(nodes.every(node => node.tag === '#text'));
  assert.equal(nodes.map(node => node.text).join(''), text);
}

test('embedded JavaScript syntax', () => { new vm.Script(script); });
test('inherited names and missing ordinary words remain text in both paths', () => {
  for (const text of ['constructor', 'toString', '__proto__', 'hasOwnProperty', 'ordinary']) {
    for (const fragment of paths(text)) assertText(render(text, {}, {}, new Map(), fragment), text);
  }
});
test('own catalog emotes render with original attributes in both paths', () => {
  for (const text of ['Kappa', 'constructor', '__proto__']) {
    const local = JSON.parse(JSON.stringify({[text]: 'https://local.invalid/emote.png'}));
    for (const fragment of paths(text)) {
      const [img] = render(text, local, {}, new Map(), fragment);
      assert.equal(img.tag, 'img'); assert.equal(img.src, local[text]);
      assert.equal(img.alt, text); assert.equal(img.title, text);
      assert.equal(img.crossOrigin, 'anonymous'); assert.equal(img.className, 'emote');
    }
  }
});
test('invalid catalog values remain text in both paths', () => {
  for (const value of [null, undefined, false, true, 42, {}, [], () => {}, '', '   ']) {
    for (const fragment of paths('Kappa')) {
      assertText(render('Kappa', {Kappa: value}, {Kappa: value}, new Map(), fragment), 'Kappa');
    }
  }
});
test('valid local, CDN and remote priorities and invalid-value fallback', () => {
  const remote = new Map([['Kappa', 'https://remote.invalid/emote.png']]);
  for (const fragment of paths('Kappa')) {
    for (const [local, cdn, expected] of [
      [{Kappa: 'local.png'}, {Kappa: 'cdn.png'}, 'local.png'],
      [{}, {Kappa: 'cdn.png'}, 'cdn.png'],
      [{Kappa: 42}, {Kappa: 'cdn.png'}, 'cdn.png'],
      [{Kappa: {}}, {}, 'https://remote.invalid/emote.png'],
    ]) assert.equal(render('Kappa', local, cdn, remote, fragment)[0].src, expected);
  }
});
test('custom inherited entries are ignored even with an own hasOwnProperty key', () => {
  const local = Object.assign(Object.create({Kappa: 'inherited.png'}), {hasOwnProperty: false});
  for (const fragment of paths('Kappa')) {
    assertText(render('Kappa', local, {}, new Map(), fragment), 'Kappa');
  }
});
test('Twitch fragment IDs keep priority over catalogs', () => {
  for (const fragment of [{text: 'Kappa', emoteID: '25'}, {text: 'Kappa', emote: {id: '25'}}]) {
    assert.equal(render('', {Kappa: 'local.png'}, {}, new Map(), fragment)[0].src,
      'https://static-cdn.jtvnw.net/emoticons/v2/25/default/dark/1.0');
  }
});
test('special characters, whitespace and Unicode stay safe text nodes', () => {
  const text = '<img onerror="synthetic"> & \\ Привет 🎮\n\t emoji';
  for (const fragment of paths(text)) assertText(render(text, {}, {}, new Map(), fragment), text);
});
