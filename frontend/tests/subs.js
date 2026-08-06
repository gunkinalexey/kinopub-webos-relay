// Drives the real app.js subtitle block against a DOM stub that models how a
// browser actually behaves: appending a <track> element adds an entry to
// video.textTracks and fires 'addtrack' on the list asynchronously.
const fs = require('fs');

let created = 0;               // how many <track> elements were built
const tasks = [];              // queued async DOM events
const queue = fn => tasks.push(fn);

function makeEl(id) {
  const el = {
    id, textContent: '', value: '', disabled: false,
    selectedIndex: 0, children: [], dataset: {}, style: {}, offsetParent: {},
    attrs: {},
    classList: { _s: new Set(), add(...c){c.forEach(x=>this._s.add(x));}, remove(...c){c.forEach(x=>this._s.delete(x));}, toggle(c,o){o?this._s.add(c):this._s.delete(c);}, contains(c){return this._s.has(c);} },
    appendChild(c) { this.children.push(c); c.parentNode = this; return c; },
    removeChild(c) { const i=this.children.indexOf(c); if(i>=0) this.children.splice(i,1); return c; },
    remove() { if (this.parentNode) this.parentNode.removeChild(this); },
    querySelector(){return null;}, querySelectorAll(){return [];},
    setAttribute(k,v){this.attrs[k]=v;}, getAttribute(k){return this.attrs[k]||null;},
    focus(){}, click(){}, scrollIntoView(){}, addEventListener(){}, removeEventListener(){},
    getBoundingClientRect(){return {left:0,top:0,width:100,height:10};},
    contains(){return false;}, load(){}, pause(){}, play(){return Promise.resolve();},
    removeAttribute(){},
  };
  // Real browsers drop existing children when innerHTML is assigned.
  let html = '';
  Object.defineProperty(el, 'innerHTML', {
    get: () => html,
    set(v) { html = v; el.children.length = 0; },
  });
  return el;
}

const els = {};
const $$ = id => (els[id] || (els[id] = makeEl(id)));

const video = $$('video');
const textTracks = [];
textTracks.onaddtrack = null; textTracks.onchange = null;
Object.assign(video, {
  paused: false, ended: false, currentTime: 0, duration: 7200, readyState: 2,
  error: null, textTracks, audioTracks: [],
  seekable: { length: 1, start: () => 0, end: () => 7200 },
  // Browser behaviour: a <track> child contributes a TextTrack and fires addtrack.
  appendChild(c) {
    this.children.push(c); c.parentNode = this;
    if (c.tagName === 'TRACK') {
      c.track = { mode: 'disabled', kind: c.kind, label: c.label, language: c.srclang };
      textTracks.push(c.track);
      queue(() => { if (typeof textTracks.onaddtrack === 'function') textTracks.onaddtrack({}); });
    }
    return c;
  },
  removeChild(c) {
    const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1);
    if (c.track) { const j = textTracks.indexOf(c.track); if (j >= 0) textTracks.splice(j, 1); }
    return c;
  },
  querySelectorAll(sel) {
    if (sel.indexOf('track[data-kp-external]') >= 0)
      return this.children.filter(c => c.tagName === 'TRACK' && c.attrs['data-kp-external']);
    return [];
  },
});

global.window = { KP_BACKEND: '/bridge' };
global.screen = { width: 1920, height: 1080 };
global.navigator = { userAgent: 'test' };
global.sessionStorage = { clear(){} }; global.localStorage = { removeItem(){} };
global.document = {
  getElementById: $$,
  createElement(t) {
    const el = makeEl('new-' + t);
    el.tagName = t.toUpperCase();
    if (t === 'track') { created++; el.track = null; }
    return el;
  },
  querySelectorAll: () => [], querySelector: () => null,
  addEventListener(){}, head: makeEl('head'), body: makeEl('body'),
  activeElement: null, title: '',
};
global.Hls = undefined;
global.KPApi = {
  status:()=>new Promise(()=>{}), settings:()=>new Promise(()=>{}), profile:()=>new Promise(()=>{}),
  report:()=>Promise.resolve(), watchingStatuses:()=>Promise.resolve({statuses:{}}),
  history:()=>Promise.resolve({items:[]}), hlsAudioVariants:()=>Promise.resolve({count:0}),
  createAudioHls:()=>new Promise(()=>{}), audioHlsStatus:()=>new Promise(()=>{}),
  stopAudioHls:()=>Promise.resolve({}),
  streamProxyUrl:u=>u, hlsProxyUrl:u=>u, imageProxyUrl:u=>u,
  subtitleProxyUrl:(u,o)=>'/bridge/subtitle?url='+encodeURIComponent(u)+(o>0?'&offset='+o:''),
};

const src = fs.readFileSync(process.argv[2], 'utf8');
eval(src.replace('}());', 'global.__app={state:state,applySubtitleChoice:applySubtitleChoice,'
  + 'populateSubtitleMenu:populateSubtitleMenu,refresh:refreshNativeTracks,'
  + 'preferred:preferredSubtitleChoice,applySize:applySubtitleSize,'
  + 'prepare:preparePlayerOptions};}());'));

const app = global.__app, st = app.state;

// Drain queued DOM events and timers the way a browser would, with a cap so a
// runaway loop terminates instead of hanging the test.
function pump(rounds) {
  for (let i = 0; i < rounds; i++) {
    const batch = tasks.splice(0);
    batch.forEach(fn => { try { fn(); } catch (e) { console.log('  threw:', e.message); } });
  }
}

let failures = 0;
const check = (name, got, want) => {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) failures++;
  console.log(`${ok?'PASS':'FAIL'}  ${name}\n        got ${JSON.stringify(got)}${ok?'':`\n        want ${JSON.stringify(want)}`}`);
};

const SUBS = [
  { url: 'https://cdn/ru.srt', lang: 'rus', shift: 0 },
  { url: 'https://cdn/en.srt', lang: 'eng', shift: 0 },
];

st.playerSubtitles = SUBS;
st.playerSubtitleChoice = 'off';
st.audioHlsActive = false; st.audioHlsOffset = 0;

console.log('--- selecting an external subtitle ---');
created = 0;
app.applySubtitleChoice('track:0');
pump(20);
console.log(`  <track> elements created: ${created}`);
check('exactly one <track> built, no rebuild loop', created, 1);
check('textTracks holds one entry', textTracks.length, 1);
check('menu lists Выкл. + 2 subtitles', els.playerSubtitles.children.length, 2);
check('cue is showing', textTracks[0].mode, 'showing');

console.log('--- switching to the other subtitle ---');
created = 0;
app.applySubtitleChoice('track:1');
pump(20);
check('one rebuild for a real change', created, 1);
check('still a single text track', textTracks.length, 1);
check('src carries the new url',
  video.children.filter(c=>c.tagName==='TRACK')[0].src.indexOf('en.srt') > 0, true);

console.log('--- turning subtitles off ---');
created = 0;
app.applySubtitleChoice('off');
pump(20);
check('no track element left', textTracks.length, 0);
check('nothing rebuilt', created, 0);

console.log('--- per-track shift is sent to the proxy ---');
st.playerSubtitles = [{ url: 'https://cdn/ru.srt', lang: 'rus', shift: 2.5 }];
st.playerSubtitleChoice = 'off'; st.subtitleMountKey = '';
app.applySubtitleChoice('track:0');
pump(5);
check('offset=2.5 in the request',
  /offset=2\.5/.test(video.children.filter(c=>c.tagName==='TRACK')[0].src), true);

console.log('--- shift adds to the local-HLS start offset ---');
st.audioHlsActive = true; st.audioHlsOffset = 10;
app.applySubtitleChoice('track:0', true);
pump(5);
check('offset=12.5 (2.5 shift + 10 hls start)',
  /offset=12\.5/.test(video.children.filter(c=>c.tagName==='TRACK')[0].src), true);
st.audioHlsActive = false; st.audioHlsOffset = 0;

console.log('--- language preference picks the track ---');
st.settings = { subtitles: 'ru', subtitle_size: 125 };
st.playerSubtitles = [{ url: 'https://cdn/en.srt', lang: 'eng' }, { url: 'https://cdn/ru.srt', lang: 'rus' }];
check('prefers the Russian track', app.preferred(), 'track:1');
st.settings.subtitles = 'off';
check('off preference stays off', app.preferred(), 'off');
st.settings.subtitles = 'ru';
st.playerSubtitles = [{ url: 'https://cdn/en.srt', lang: 'eng' }];
check('no match falls back to off', app.preferred(), 'off');
st.playerSubtitles = [{ lang: 'rus', embed: true }];
check('unusable track is not auto-selected', app.preferred(), 'off');

console.log('--- embedded-only track is offered, not hidden ---');
st.playerSubtitles = [];
textTracks.length = 0;
textTracks.push({ mode: 'disabled', kind: 'subtitles', label: 'Forced', language: 'rus' });
app.populateSubtitleMenu();
check('embedded track appears in the menu', els.playerSubtitles.children.length, 1);
check('and is selectable', els.playerSubtitles.children[0].disabled, false);

console.log('--- size control maps to a class ---');
const layer = els.playerLayer;
app.applySize(125);
check('subs-125 applied', layer.classList.contains('subs-125'), true);
app.applySize(150);
check('previous size class removed', layer.classList.contains('subs-125'), false);
check('subs-150 applied', layer.classList.contains('subs-150'), true);
app.applySize('nonsense');
check('garbage falls back to 100', layer.classList.contains('subs-100'), true);

console.log(failures ? `\n${failures} FAILURE(S)` : '\nAll checks passed');
process.exit(failures ? 1 : 0);
