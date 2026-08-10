// Loads the real frontend/app.js against a stubbed DOM and drives the audio
// switching ladder. Verifies which tier handles a selection for each stream
// shape, without needing a browser or KinoPub credentials.
const fs = require('fs');

function makeEl(id) {
  const el = {
    id, innerHTML: '', textContent: '', value: '', disabled: false,
    selectedIndex: 0, children: [], dataset: {}, style: {}, offsetParent: {},
    classList: {
      _s: new Set(),
      add(...c) { c.forEach(x => this._s.add(x)); },
      remove(...c) { c.forEach(x => this._s.delete(x)); },
      toggle(c, on) { on ? this._s.add(c) : this._s.delete(c); },
      contains(c) { return this._s.has(c); },
    },
    appendChild(c) { this.children.push(c); return c; },
    removeChild(c) { return c; },
    remove() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    setAttribute() {}, getAttribute() { return null; },
    focus() {}, click() {}, scrollIntoView() {},
    addEventListener() {}, removeEventListener() {},
    getBoundingClientRect() { return { left: 0, top: 0, width: 100, height: 10 }; },
    contains() { return false; },
    load() {}, pause() {}, play() { return Promise.resolve(); },
    removeAttribute() {},
  };
  return el;
}

const els = {};
const $$ = id => (els[id] || (els[id] = makeEl(id)));

// Video element, with switchable track shapes.
const video = $$('video');
Object.assign(video, {
  paused: false, ended: false, currentTime: 0, duration: 7200, readyState: 2,
  error: null, textTracks: [], audioTracks: [],
  seekable: { length: 1, start: () => 0, end: () => 7200 },
});

global.window = { KP_BACKEND: '/bridge' };
global.screen = { width: 1920, height: 1080 };
global.navigator = { userAgent: 'test' };
global.sessionStorage = { clear() {} };
global.localStorage = { removeItem() {} };
global.document = {
  getElementById: $$,
  createElement: t => makeEl('new-' + t),
  querySelectorAll: () => [],
  querySelector: () => null,
  addEventListener() {}, head: makeEl('head'), body: makeEl('body'),
  activeElement: null, title: '',
};
global.Hls = undefined;

// --- test instrumentation -------------------------------------------------
const calls = [];
global.KPApi = {
  status: () => new Promise(() => {}),           // never resolves: skip boot
  settings: () => new Promise(() => {}),
  profile: () => new Promise(() => {}),
  report: () => Promise.resolve(),
  watchingStatuses: () => Promise.resolve({ statuses: {} }),
  history: () => Promise.resolve({ items: [] }),
  hlsAudioVariants(url) {
    calls.push(['probe', url]);
    return Promise.resolve({ url, count: (global.RENDITIONS || {})[url] || 0 });
  },
  createAudioHls(url, track, start) {
    calls.push(['ffmpeg', track, Math.round(start)]);
    return Promise.resolve({ job_id: 'j1', status: 'starting' });
  },
  audioHlsStatus: () => new Promise(() => {}),
  stopAudioHls: () => Promise.resolve({}),
  streamProxyUrl: u => '/bridge/stream?url=' + encodeURIComponent(u),
  hlsProxyUrl: u => '/bridge/hls?url=' + encodeURIComponent(u),
  subtitleProxyUrl: u => '/bridge/subtitle?url=' + encodeURIComponent(u),
  imageProxyUrl: u => u,
};

const src = fs.readFileSync(process.argv[2], 'utf8');
// Expose internals for assertions: the IIFE keeps everything private.
eval(src.replace('}());', 'global.__app={state:state,applyAudioChoice:applyAudioChoice,'
  + 'populateAudioMenu:populateAudioMenu,openUrl:openUrl,reapply:reapplyAudioSelection,'
  + 'firstNonEmptyList:firstNonEmptyList};}());'));

const app = global.__app;
const st = app.state;

function reset(audios, opts) {
  opts = opts || {};
  calls.length = 0;
  st.playerAudios = audios;
  st.playerAudioChoice = 'auto';
  st.audioHlsActive = false; st.audioHlsPreparing = false;
  st.pendingAltAudioIndex = -1; st.altAudioUrl = ''; st.altAudioProbe = {};
  st.streamSwitchSeq = 1; st.hlsManifestReady = true;
  st.playerResumePosition = 0; video.currentTime = 0;
  st.playerStreams = [{ variants: opts.variants || { http: 'http://x/f.mp4', hls: 'http://x/f.m3u8' } }];
  st.playerQualityIndex = '0';
  video.audioTracks = opts.native || [];
  st.hls = opts.hlsTracks ? { audioTracks: opts.hlsTracks, audioTrack: 0 } : null;
  global.RENDITIONS = opts.renditions || {};
}

const AUDIOS = [
  { index: 1, lang: 'rus', author: { title: 'LostFilm' } },
  { index: 2, lang: 'rus', author: { title: 'HDrezka' } },
  { index: 3, lang: 'eng' },
];

let failures = 0;
function check(name, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) failures++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}\n        got ${JSON.stringify(got)}${ok ? '' : `\n        want ${JSON.stringify(want)}`}`);
}

const done = () => new Promise(r => setTimeout(r, 30));

(async () => {
  // 1. Manifest already carries renditions -> switch in place, no network.
  reset(AUDIOS, { hlsTracks: [{ name: 'ru' }, { name: 'ru2' }, { name: 'en' }] });
  app.applyAudioChoice('track:2'); await done();
  check('tier1 hls.js alt-audio: no probe, no ffmpeg', calls, []);
  check('tier1 selected hls audioTrack', st.hls.audioTrack, 2);

  // 2. Native MP4 tracks (webOS direct playback).
  const nat = [{ enabled: true }, { enabled: false }, { enabled: false }];
  reset(AUDIOS, { native: nat });
  app.applyAudioChoice('track:1'); await done();
  check('tier2 native tracks: no probe, no ffmpeg', calls, []);
  check('tier2 enabled flags', nat.map(t => t.enabled), [false, true, false]);

  // 3. Loaded variant is single-track but hls4 has renditions -> reload it.
  reset(AUDIOS, {
    variants: { http: 'http://x/f.mp4', hls: 'http://x/f.m3u8', hls4: 'http://x/f4.m3u8' },
    renditions: { 'http://x/f4.m3u8': 3 },
  });
  app.applyAudioChoice('track:2'); await done();
  check('tier3 probed hls4 only', calls, [['probe', 'http://x/f4.m3u8']]);
  check('tier3 reloaded alt variant', st.streamUrl, 'http://x/f4.m3u8');
  check('tier3 remembered wanted track', st.pendingAltAudioIndex, 2);
  check('tier3 did not start ffmpeg', calls.filter(c => c[0] === 'ffmpeg'), []);

  // 4. Nothing anywhere -> ffmpeg, addressed by ORDINAL not audios[].index.
  reset(AUDIOS, {
    variants: { http: 'http://x/f.mp4', hls: 'http://x/f.m3u8', hls4: 'http://x/f4.m3u8' },
    renditions: {},
  });
  st.playerResumePosition = 1800; video.currentTime = 1800;
  app.applyAudioChoice('track:2'); await done();
  check('tier4 probed both variants then gave up',
    calls.filter(c => c[0] === 'probe').length, 2);
  check('tier4 ffmpeg gets 0-based ordinal + position',
    calls.find(c => c[0] === 'ffmpeg'), ['ffmpeg', 2, 1800]);

  // 5. Regression: 0-based audios[].index must not collapse track 2 onto 1.
  reset([{ index: 0, lang: 'rus' }, { index: 1, lang: 'eng' }], { renditions: {} });
  video.currentTime = 0;
  app.applyAudioChoice('track:1'); await done();
  check('0-based index payload still selects the 2nd track',
    calls.find(c => c[0] === 'ffmpeg'), ['ffmpeg', 1, 0]);

  // 6. Regression: empty array must not shadow the media node's list.
  check('firstNonEmptyList skips empty array',
    app.firstNonEmptyList([], [{ lang: 'rus' }, { lang: 'eng' }]).length, 2);

  // 7. Menu must list every API track even when the stream exposes one.
  reset(AUDIOS, { native: [{ enabled: true }] });
  app.populateAudioMenu();
  check('menu lists all API tracks + Auto', els.playerAudio.children.length, 3);

  // 8. A backend built with WITH_FFMPEG=0 has no ffmpeg in the image at all,
  //    so the last rung of the ladder must be refused here rather than sent
  //    off to come back 503. The three rungs above it are untouched by this.
  reset(AUDIOS, {});
  st.serverFfmpeg = false;
  calls.length = 0;
  app.applyAudioChoice('track:1');
  await done();
  check('no remux is even requested without FFmpeg',
    calls.filter(c => c[0] === 'ffmpeg').length, 0);
  check('and the user is told why', /без FFmpeg/.test(els.playerError.textContent), true);
  check('the previous track stays selected', st.playerAudioChoice, 'auto');

  //    Unknown (health unreachable) must behave exactly as before, not
  //    disable a feature that probably works.
  reset(AUDIOS, {});
  st.serverFfmpeg = null;
  calls.length = 0;
  app.applyAudioChoice('track:1');
  await done();
  check('an unknown backend still tries the remux',
    calls.filter(c => c[0] === 'ffmpeg').length, 1);
  st.serverFfmpeg = null;

  console.log(failures ? `\n${failures} FAILURE(S)` : '\nAll checks passed');
  process.exit(failures ? 1 : 0);
})();
