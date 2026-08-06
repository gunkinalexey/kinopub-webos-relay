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

const HEVC='video/mp4; codecs="hvc1.1.6.L150.B0"', H264='video/mp4; codecs="avc1.640028"';
global.DEVICE={};

const src=fs.readFileSync(process.argv[2],'utf8');
eval(src.replace('}());','global.__app={state:state,prepare:preparePlayerOptions,'
  +'enterFs:enterPlayerFullscreen,fsMode:playerFullscreenMode,play:play,closePlayer:closePlayer,'
  +'caps:mediaCapabilities,bestIndex:bestPlayableGroupIndex,mode:preferredModeFor,'
  +'group:currentQualityGroup,openUrl:openUrl,mediaError:mediaError};}());'));
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

console.log('--- LG NanoCell: HEVC in hardware, HDR panel ---');
setup({canPlay:{[HEVC]:'probably',[H264]:'probably'},mse:{[HEVC]:true,[H264]:true},
       media:{'(dynamic-range: high)':true,'(color-gamut: p3)':true}});
check('picks 2160p HEVC', picked(), '2160p HDR hevc');
check('hands the file to the TV decoder, not MSE', app.mode(app.group()), 'direct');
check('reports the HDR panel', app.caps().hdrDisplay, true);

console.log('--- desktop Chrome: no HEVC ---');
setup({canPlay:{[HEVC]:'',[H264]:'probably'},mse:{[H264]:true},media:{}});
check('falls back to the best H.264', picked(), '1080p h264');
check('uses hls for H.264', app.mode(app.group()), 'hls');

console.log('--- HEVC only through MSE (no direct decode) ---');
setup({canPlay:{[HEVC]:'',[H264]:'probably'},mse:{[HEVC]:true,[H264]:true},media:{}});
check('still selects 2160p HEVC', picked(), '2160p HDR hevc');
check('but goes through hls.js since direct cannot decode it', app.mode(app.group()), 'hls');

console.log('--- quality ceiling from settings ---');
setup({canPlay:{[HEVC]:'probably',[H264]:'probably'},mse:{[HEVC]:true},media:{}},{quality:'1080'});
check('1080 ceiling skips the 4K variant', picked(), '1080p h264');
setup({canPlay:{[HEVC]:'probably',[H264]:'probably'},mse:{[HEVC]:true},media:{}},{quality:'720'});
check('720 ceiling', picked(), '720p h264');
setup({canPlay:{[HEVC]:'probably',[H264]:'probably'},mse:{[HEVC]:true},media:{}},{quality:'2160'});
check('2160 ceiling keeps 4K', picked(), '2160p HDR hevc');

console.log('--- explicit stream mode still wins ---');
setup({canPlay:{[HEVC]:'probably',[H264]:'probably'},mse:{[HEVC]:true},media:{}},{stream_mode:'relay'});
check('manual relay is respected', app.mode(app.group()), 'relay');

console.log('--- direct playback failure falls back once ---');
setup({canPlay:{[HEVC]:'probably',[H264]:'probably'},mse:{[HEVC]:true},media:{}});
st.hdrAttempt=true; st.hdrFellBack=false; st.mode='direct'; st.episodeId='';
$$('playerError').textContent='';
app.mediaError('Формат не поддерживается');
check('switched away from direct', st.mode !== 'direct', true);
check('told the user why', /переключаемся через сервер/.test($$('playerError').textContent), true);
const before=st.mode;
app.mediaError('Формат не поддерживается');
check('does not loop on a second failure', st.mode, before);

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
