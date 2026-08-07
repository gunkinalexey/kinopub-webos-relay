// Which variant and transport the player picks for a given device.
const fs = require('fs');

function makeEl(id) {
  const el = { id, textContent:'', value:'', disabled:false, selectedIndex:0, children:[],
    dataset:{}, style:{}, offsetParent:{}, attrs:{},
    classList:{_s:new Set(),add(...c){c.forEach(x=>this._s.add(x));},remove(...c){c.forEach(x=>this._s.delete(x));},
      toggle(c,o){o?this._s.add(c):this._s.delete(c);},contains(c){return this._s.has(c);}},
    appendChild(c){this.children.push(c);c.parentNode=this;return c;},
    removeChild(c){const i=this.children.indexOf(c);if(i>=0)this.children.splice(i,1);return c;},
    remove(){}, querySelector(){return null;}, querySelectorAll(){return [];},
    setAttribute(k,v){this.attrs[k]=v;}, getAttribute(k){return this.attrs[k]||null;},
    focus(){}, click(){}, scrollIntoView(){}, addEventListener(){}, removeEventListener(){},
    getBoundingClientRect(){return {left:0,top:0,width:100,height:10};},
    contains(){return false;}, load(){}, removeAttribute(){} };
  let html=''; Object.defineProperty(el,'innerHTML',{get:()=>html,set(v){html=v;el.children.length=0;}});
  return el;
}
const els={}, $$=id=>(els[id]||(els[id]=makeEl(id)));
const video=$$('video');
const videoListeners={};
video.addEventListener=(type,fn)=>{(videoListeners[type]=videoListeners[type]||[]).push(fn);};
video.removeEventListener=(type,fn)=>{const l=videoListeners[type];if(!l)return;const i=l.indexOf(fn);if(i>=0)l.splice(i,1);};
video.__fire=(type)=>{(videoListeners[type]||[]).slice().forEach(fn=>fn());};
Object.assign(video,{paused:false,ended:false,currentTime:0,duration:7200,readyState:2,error:null,
  textTracks:[],audioTracks:[],seekable:{length:1,start:()=>0,end:()=>7200},
  pause(){this.paused=true;},play(){this.paused=false;return Promise.resolve();},
  canPlayType(m){ return (global.DEVICE.canPlay||{})[m] || ''; }});

global.window={KP_BACKEND:'/bridge',
  get MediaSource(){ return global.DEVICE.mse ? {isTypeSupported:m=>!!global.DEVICE.mse[m]} : undefined; },
  matchMedia:q=>({matches:!!(global.DEVICE.media||{})[q]})};
global.matchMedia = global.window.matchMedia;
Object.defineProperty(global,'MediaSource',{get(){return global.window.MediaSource;},configurable:true});
global.screen={width:3840,height:2160}; global.navigator={userAgent:'webOS'};
global.sessionStorage={clear(){}}; global.localStorage={removeItem(){}};
global.document={getElementById:$$,createElement:t=>{const e=makeEl('new-'+t);e.tagName=t.toUpperCase();
  if(t==='video')e.canPlayType=m=>(global.DEVICE.canPlay||{})[m]||''; return e;},
  querySelectorAll:()=>[],querySelector:()=>null,addEventListener(){},head:makeEl('head'),body:makeEl('body'),
  activeElement:null,title:''};
global.Hls=undefined;
const opened=[];
global.KPApi={status:()=>new Promise(()=>{}),settings:()=>new Promise(()=>{}),profile:()=>new Promise(()=>{}),
  report:()=>Promise.resolve(),watchingStatuses:()=>Promise.resolve({statuses:{}}),history:()=>Promise.resolve({items:[]}),
  hlsAudioVariants:()=>Promise.resolve({count:0}),createAudioHls:()=>new Promise(()=>{}),
  audioHlsStatus:()=>new Promise(()=>{}),stopAudioHls:()=>Promise.resolve({}),saveProgress:()=>Promise.resolve({}),
  streamProxyUrl:u=>'/bridge/stream?'+u,hlsProxyUrl:u=>'/bridge/hls?'+u,imageProxyUrl:u=>u,subtitleProxyUrl:u=>u};

// Probe strings the app actually asks about. Level is part of the question:
// L120/L4.0 are the 1080p-capable levels, L150/L5.1 the 2160p ones, so a
// device can answer yes to one and no to the other.
const HEVC='video/mp4; codecs="hvc1.1.6.L120.B0"', HEVC4K='video/mp4; codecs="hvc1.1.6.L150.B0"';
const H264='video/mp4; codecs="avc1.640028"', H2644K='video/mp4; codecs="avc1.640033"';
global.DEVICE={};

const src=fs.readFileSync(process.argv[2],'utf8');
eval(src.replace('}());','global.__app={state:state,prepare:preparePlayerOptions,'
  +'enterFs:enterPlayerFullscreen,fsMode:playerFullscreenMode,play:play,closePlayer:closePlayer,'
  +'caps:mediaCapabilities,bestIndex:bestPlayableGroupIndex,mode:preferredModeFor,'
  +'group:currentQualityGroup,openUrl:openUrl,mediaError:mediaError,'
  +'reported:reportedCapabilities,watchStall:watchDirectStall,'
  +'applyHlsLevel:applyHlsLevelPreference,levelForHeight:hlsLevelForHeight,switchQuality:switchQuality,'
  +'qualityMenu:function(){return els.playerQuality.children.map(function(o){return o.textContent;});}};}());'));
const app=global.__app, st=app.state;

const STREAMS=[
  {url:'http://cdn/4k.mp4',   source_type:'http', protocol:'http', codec:'hevc', height:2160, quality:'2160p HDR', file:'4k'},
  {url:'http://cdn/4k.m3u8',  source_type:'hls',  protocol:'hls',  codec:'hevc', height:2160, quality:'2160p HDR', file:'4k'},
  {url:'http://cdn/1080.mp4', source_type:'http', protocol:'http', codec:'h264', height:1080, quality:'1080p', file:'fhd'},
  {url:'http://cdn/1080.m3u8',source_type:'hls',  protocol:'hls',  codec:'h264', height:1080, quality:'1080p', file:'fhd'},
  {url:'http://cdn/720.m3u8', source_type:'hls',  protocol:'hls',  codec:'h264', height:720,  quality:'720p', file:'hd'},
];

function setup(device, settings){
  global.DEVICE=device; st.mediaCaps=null;
  st.settings=Object.assign({stream_mode:'auto',quality:'auto'},settings||{});
  opened.length=0;
  app.prepare({streams:STREAMS,subtitles:[],audios:[]},STREAMS[0]);
}
const picked=()=>{const g=app.group();return g?(g.quality+' '+g.codec):'none';};

let failures=0;
const check=(n,got,want)=>{const ok=JSON.stringify(got)===JSON.stringify(want);if(!ok)failures++;
  console.log(`${ok?'PASS':'FAIL'}  ${n}\n        got ${JSON.stringify(got)}${ok?'':`\n        want ${JSON.stringify(want)}`}`);};

// Full HEVC device: decodes HEVC at both 1080p and 4K levels, in hardware.
const UHD_HEVC={canPlay:{[HEVC]:'probably',[HEVC4K]:'probably',[H264]:'probably',[H2644K]:'probably'},
  mse:{[HEVC]:true,[HEVC4K]:true,[H264]:true,[H2644K]:true},media:{}};
const dev=extra=>Object.assign({},UHD_HEVC,extra||{});

console.log('--- LG NanoCell: HEVC in hardware, HDR panel ---');
setup(dev({media:{'(dynamic-range: high)':true,'(color-gamut: p3)':true}}));
check('picks 2160p HEVC', picked(), '2160p HDR hevc');
check('hands the file to the TV decoder, not MSE', app.mode(app.group()), 'direct');
check('reports the HDR panel', app.caps().hdrDisplay, true);
check('tells KinoPub it can take HEVC/4K/HDR', app.reported(), {hevc:true,uhd:true,hdr:true});

console.log('--- desktop Chrome: no HEVC ---');
setup({canPlay:{[H264]:'probably',[H2644K]:'probably'},mse:{[H264]:true,[H2644K]:true},media:{}});
check('falls back to the best H.264', picked(), '1080p h264');
check('uses hls for H.264', app.mode(app.group()), 'hls');
check('does not claim HEVC to KinoPub', app.reported().hevc, false);
check('the undecodable 4K HEVC entry is not offered at all', app.qualityMenu(), ['1080p · H264 · HLS','720p · H264 · HLS']);

console.log('--- HEVC only through MSE (no direct decode) ---');
setup({canPlay:{[H264]:'probably'},mse:{[HEVC]:true,[HEVC4K]:true,[H264]:true},media:{}});
check('still selects 2160p HEVC', picked(), '2160p HDR hevc');
check('but goes through hls.js since direct cannot decode it', app.mode(app.group()), 'hls');
check('MSE-only HEVC still counts as HEVC support', app.reported().hevc, true);

console.log('--- HEVC at 1080p but not at 4K level ---');
setup({canPlay:{[HEVC]:'probably',[H264]:'probably'},mse:{[HEVC]:true,[H264]:true},media:{}});
check('reports HEVC yes, 4K no', app.reported(), {hevc:true,uhd:false,hdr:false});

console.log('--- nothing decodable at all: still offer something, do not go silent ---');
setup({canPlay:{},mse:{},media:{}});
check('falls back to the largest variant rather than the smallest', picked(), '2160p HDR hevc');
check('every entry is kept, each marked unsupported', app.qualityMenu().length, 3);
check('and they say so', /не поддерживается/.test(app.qualityMenu()[0]), true);

console.log('--- quality ceiling from settings ---');
setup(dev(),{quality:'1080'});
check('1080 ceiling skips the 4K variant', picked(), '1080p h264');
setup(dev(),{quality:'720'});
check('720 ceiling', picked(), '720p h264');
setup(dev(),{quality:'2160'});
check('2160 ceiling keeps 4K', picked(), '2160p HDR hevc');
// The ceiling is a preference, not a hard limit: if it excludes everything,
// play the best decodable variant instead of refusing to play at all.
setup(dev(),{quality:'240'});
check('an impossible ceiling still plays the best decodable variant', picked(), '2160p HDR hevc');

console.log('--- explicit stream mode still wins ---');
setup(dev(),{stream_mode:'relay'});
check('manual relay is respected', app.mode(app.group()), 'relay');

console.log('--- direct playback failure falls back once ---');
setup(dev());
st.hdrAttempt=true; st.hdrFellBack=false; st.mode='direct'; st.episodeId='';
$$('playerError').textContent='';
app.mediaError('Формат не поддерживается');
check('switched away from direct', st.mode !== 'direct', true);
check('told the user why', /переключаемся через сервер/.test($$('playerError').textContent), true);
const before=st.mode;
app.mediaError('Формат не поддерживается');
check('does not loop on a second failure', st.mode, before);

// A direct stream that will not start is worth relaying regardless of why
// it was picked, so the fallback no longer requires the HDR-attempt flag.
console.log('--- direct failure without the HDR flag still falls back ---');
setup(dev());
st.hdrAttempt=false; st.hdrFellBack=false; st.mode='direct'; st.episodeId='';
app.mediaError('Сетевая ошибка');
check('plain direct failure is relayed too', st.mode !== 'direct', true);

// A stalled direct start fires no `error` event at all, so only a watchdog
// catches it. Verified against the real bug: 4K resumed at a position sat
// at readyState 1 with an empty buffer indefinitely.
console.log('--- direct start that stalls with an empty buffer ---');
// The watchdog is a 12s timer; drive it by hand instead of waiting. Scoped
// to this section so the rest of the file keeps real timers.
const realSetTimeout=global.setTimeout, realClearTimeout=global.clearTimeout;
let pending=[];
global.setTimeout=(fn)=>{const t={fn};pending.push(t);return t;};
global.clearTimeout=(t)=>{const i=pending.indexOf(t);if(i>=0)pending.splice(i,1);};
app.runTimers=()=>{const q=pending;pending=[];q.forEach(t=>{try{t.fn();}catch(e){}});};
function stallSetup(){ setup(dev()); pending=[]; st.hdrFellBack=false; st.mode='direct'; st.episodeId='';
  st.streamSwitchSeq=(st.streamSwitchSeq||0); video.readyState=1; video.buffered={length:0}; }
stallSetup();
app.watchStall(st.streamSwitchSeq);
app.runTimers();
check('empty buffer after the grace period is relayed', st.mode !== 'direct', true);

stallSetup(); video.readyState=4; video.buffered={length:1,start:()=>0,end:()=>5};
app.watchStall(st.streamSwitchSeq);
app.runTimers();
check('a healthy direct start is left alone', st.mode, 'direct');

stallSetup(); video.readyState=1; video.buffered={length:0};
app.watchStall(st.streamSwitchSeq);
st.streamSwitchSeq++;               // user switched quality/stream meanwhile
app.runTimers();
check('a superseded stream is not yanked out from under the new one', st.mode, 'direct');
video.readyState=2; video.buffered={length:1,start:()=>0,end:()=>7200};

// The regression this section exists for: a real 4K/HDR file on a real TV's
// real network can easily take longer than one grace window to produce its
// first buffered range while working perfectly fine - the first version of
// this watchdog used one fixed deadline with no way to tell that apart from
// "stuck", and yanked a healthy-but-slow direct/HDR-capable stream over to
// relay (which drops HDR on webOS) for nothing. `progress` events are the
// signal that distinguishes them.
console.log('--- slow but alive: `progress` events buy more time instead of forcing relay ---');
stallSetup();
app.watchStall(st.streamSwitchSeq);
video.__fire('progress');           // a byte arrived - still not enough to play, but not stuck either
app.runTimers();                    // first deadline: sees progress, extends patience instead of relaying
check('progress before the deadline keeps it on direct', st.mode, 'direct');
check('a second grace window was armed', pending.length, 1);
app.runTimers();                    // second deadline, no further progress this time: genuinely stuck now
check('no progress in the second window relays after all', st.mode !== 'direct', true);

global.setTimeout=realSetTimeout; global.clearTimeout=realClearTimeout;

// Every KinoPub HLS "quality" link is the same master listing all
// renditions, so opening at maximum is an hls.js level decision, not a URL
// choice. Left to itself ABR ramps up from a cautious guess and a 4K title
// opens at 1080p.
console.log('--- HLS renditions: open at the best the device allows ---');
const HEVC_CODECS='hvc1.2.4.L150.B0,mp4a.40.2';
function fakeHls(codecs){
  return {levels:[{height:406,codecs:codecs},{height:720,codecs:codecs},
                  {height:1080,codecs:codecs},{height:2160,codecs:codecs}],
          startLevel:-1,nextLevel:-1,currentLevel:-1,autoLevelCapping:-1};
}
function hlsSetup(settings,codecs){ setup(dev(),settings); st.mode='hls';
  st.hls=fakeHls(codecs||HEVC_CODECS); st.audioHlsActive=false; st.audioHlsPreparing=false; return st.hls; }

let h=hlsSetup({quality:'auto'});
check('starts on the 2160p rendition, not ABR\'s cautious guess', app.applyHlsLevel(false), 3);
check('first fragment is pinned to it', h.startLevel, 3);
check('ABR stays free to adapt down when no ceiling is set', h.autoLevelCapping, -1);

h=hlsSetup({quality:'1080'});
check('a 1080 ceiling starts on the 1080p rendition', app.applyHlsLevel(false), 2);
check('and ABR may never exceed it', h.autoLevelCapping, 2);

h=hlsSetup({quality:'auto'},'avc1.640028,mp4a.40.2');
check('H.264 renditions are fine on an HEVC device too', app.applyHlsLevel(false), 3);

// A device with no HEVC must not be handed HEVC renditions.
global.DEVICE={canPlay:{[H264]:'probably'},mse:{[H264]:true},media:{}}; st.mediaCaps=null;
h=st.hls=fakeHls(HEVC_CODECS); st.mode='hls'; st.settings={quality:'auto',stream_mode:'auto'};
check('no decodable rendition at all -> nothing forced', app.applyHlsLevel(false), -1);

console.log('--- picking a quality by hand moves the hls.js level ---');
h=hlsSetup({quality:'auto'});
st.playerStreams=[{height:2160,codec:'hevc',variants:{hls:'m'}},{height:1080,codec:'hevc',variants:{hls:'m'}},
                  {height:720,codec:'hevc',variants:{hls:'m'}}];
st.streamUrl='m';
app.switchQuality(2);
check('720p entry selects the 720p rendition', h.currentLevel, 1);
check('and pins ABR there rather than reloading the identical manifest', h.autoLevelCapping, 1);
check('the selection is remembered', st.playerQualityIndex, '2');
check('exact-height match wins', app.levelForHeight(1080), 2);
check('an unlisted height falls to the nearest one below', app.levelForHeight(1440), 2);
st.hls=null; st.mode='direct';

console.log('--- fullscreen ---');
const fsCalls = [];
document.fullscreenElement = null;
document.exitFullscreen = () => { fsCalls.push('exit'); document.fullscreenElement = null; return Promise.resolve(); };
const mkReq = name => function(){ fsCalls.push(name); document.fullscreenElement = this; return Promise.resolve(); };
video.requestFullscreen = mkReq('video');
$$('playerLayer').requestFullscreen = mkReq('layer');

function fsSetup(mode){ fsCalls.length=0; document.fullscreenElement=null;
  st.settings=Object.assign({stream_mode:'auto',quality:'auto'},{player_fullscreen:mode}); }

fsSetup('layer'); app.enterFs();
check('layer mode fullscreens the shell, keeping our controls', fsCalls, ['layer']);

fsSetup('video'); app.enterFs();
check('video mode fullscreens the media element', fsCalls, ['video']);

fsSetup('off'); app.enterFs();
check('off mode requests nothing', fsCalls, []);

fsSetup('layer'); app.enterFs(); app.enterFs();
check('does not re-request while already fullscreen', fsCalls, ['layer']);

fsSetup(undefined); check('unknown value falls back to layer', app.fsMode(), 'layer');
fsSetup('bogus');   check('bogus value falls back to layer', app.fsMode(), 'layer');

fsSetup('video'); app.enterFs(); fsCalls.length=0;
app.closePlayer();
check('closing the player leaves fullscreen', fsCalls, ['exit']);

// a rejected request must not throw into the click handler
fsSetup('video'); video.requestFullscreen = () => Promise.reject(new Error('denied'));
let threw = false; try { app.enterFs(); } catch (e) { threw = true; }
check('a refused request does not break playback start', threw, false);

console.log(failures?`\n${failures} FAILURE(S)`:'\nAll checks passed');
process.exit(failures?1:0);
