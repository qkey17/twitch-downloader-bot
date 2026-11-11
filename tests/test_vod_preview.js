const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const template=fs.readFileSync(path.join(__dirname,'../src/viewer_template.html'),'utf8');
const start=template.indexOf('function safeVodUrl(');
const code=template.slice(start,template.indexOf('requestAnimationFrame(fitVodTitle);',start));
const valid='https://www.twitch.tv/videos/123';
const cover='data:image/jpeg;base64,AAAA';

function setup(vod_url, embeddedCover='', frames=false){
  const opened=[];
  const button={hidden:true,addEventListener(name,fn){this[name]=fn;}};
  const card={hidden:true,closest:()=>({classList:{add(){}}})};
  const image={};
  const ctx=vm.createContext({meta:{vod_url,thumbnail_preview_url:'https://static-cdn.jtvnw.net/cf_vods/test.jpg'},
    URL,embeddedCover,hasVodImage:!!embeddedCover||frames,elVodCard:card,elVodPreviewImg:image,
    document:{getElementById:()=>button},window:{open:(...args)=>opened.push(args)}});
  vm.runInContext(code,ctx);
  return {button,card,image,opened,ctx};
}

test('VOD action remains available without an image and opens a safe new tab',()=>{
  const {button,card,image,opened}=setup(valid);
  assert.equal(button.hidden,false);
  assert.equal(card.hidden,true);
  assert.equal(image.src,undefined);
  button.click();
  assert.deepEqual(opened,[[valid,'_blank','noopener,noreferrer']]);
});

test('unsafe VOD links have no action',()=>{
  for(const url of [undefined,'javascript:alert(1)','data:text/html,test','https://twitch.tv.evil.invalid/videos/123',
    'https://user:pass@www.twitch.tv/videos/123','http://www.twitch.tv/videos/123','//www.twitch.tv/videos/123',{}]){
    const {button,opened}=setup(url);
    assert.equal(button.hidden,true);
    assert.equal(button.click,undefined);
    assert.deepEqual(opened,[]);
  }
});

test('cover is embedded, storyboard waits for the visible row, and external thumbnails stay unused',()=>{
  assert.equal(setup(valid,cover).image.src,cover);
  const {card,image,ctx}=setup(valid,'',true);
  assert.equal(card.hidden,false);
  assert.equal(image.src,undefined);
  assert.equal(ctx.embeddedJpeg(cover),cover);
  for(const value of ['https://static-cdn.jtvnw.net/cf_vods/test.jpg','data:image/svg+xml;base64,AAAA','javascript:test'])
    assert.equal(ctx.embeddedJpeg(value),'');
});
