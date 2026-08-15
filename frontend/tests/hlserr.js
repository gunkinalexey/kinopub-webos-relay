// What happens after hls.js reports an error. The library stops loading on a
// fatal one and waits for the page to restart it; this pins down that the page
// actually does, how many times, and what it falls back to when it cannot.
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
const playerError=$$('playerError');
const video=$$('video');
video.addEventListener=()=>{}; video.removeEventListener=()=>{};
Object.assign(video,{paused:false,ended:false,currentTime:0,duration:7200,readyState:2,error:null,
  textTracks:[],audioTracks:[],seekable:{length:1,start:()=>0,end:()=>7200},
  pause(){this.paused=true;},play(){this.paused=false;return Promise.resolve();},
  canPlayType(){return 'probably';}});

global.window={KP_BACKEND:'/bridge',MediaSource:{isTypeSupported:()=>true},
  matchMedia:q=>({matches:false,media:'not all'})};
global.matchMedia=global.window.matchMedia;
Object.defineProperty(global,'MediaSource',{get(){return global.window.MediaSource;},configurable:true});
global.screen={width:3840,height:2160}; global.navigator={userAgent:'webOS'};
global.sessionStorage={clear(){}}; global.localStorage={removeItem(){}};
global.document={getElementById:$$,createElement:t=>{const e=makeEl('new-'+t);e.tagName=t.toUpperCase();
  if(t==='video')e.canPlayType=()=>'probably'; return e;},
  querySelectorAll:()=>[],querySelector:()=>null,addEventListener(){},head:makeEl('head'),body:makeEl('body'),
  activeElement:null,title:''};
global.Hls=undefined;

// Controllable clock: the network rung backs off before retrying, so every
// assertion about a retry has to be able to say "and now the timer fired".
let timerSeq=0; const timers=new Map();
global.setTimeout=(fn)=>{const id=++timerSeq;timers.set(id,fn);return id;};
global.clearTimeout=id=>{timers.delete(id);};
global.setInterval=()=>0; global.clearInterval=()=>{};
const runTimers=()=>{const pending=[...timers.values()];timers.clear();pending.forEach(fn=>fn());};

const reports=[];
global.KPApi={status:()=>new Promise(()=>{}),settings:()=>new Promise(()=>{}),profile:()=>new Promise(()=>{}),
  report:(message,details,kind)=>{reports.push({message,details,kind});return Promise.resolve();},
  watchingStatuses:()=>Promise.resolve({statuses:{}}),history:()=>Promise.resolve({items:[]}),
  hlsAudioVariants:()=>Promise.resolve({count:0}),createAudioHls:()=>new Promise(()=>{}),
  audioHlsStatus:()=>new Promise(()=>{}),stopAudioHls:()=>Promise.resolve({}),saveProgress:()=>Promise.resolve({}),
  streamProxyUrl:u=>'/bridge/stream?'+u,hlsProxyUrl:u=>'/bridge/hls?'+u,imageProxyUrl:u=>u,subtitleProxyUrl:u=>u};

const src=fs.readFileSync(process.argv[2],'utf8');
eval(src.replace('}());','global.__app={state:state,prepare:preparePlayerOptions,'
  +'onError:handleHlsError,progress:noteHlsProgress,reset:resetHlsRecovery};}());'));
const app=global.__app, st=app.state;

let failures=0;
const check=(n,got,want)=>{const ok=JSON.stringify(got)===JSON.stringify(want);if(!ok)failures++;
  console.log(`${ok?'PASS':'FAIL'}  ${n}\n        got ${JSON.stringify(got)}${ok?'':`\n        want ${JSON.stringify(want)}`}`);};

// One title, offered both ways - which is what makes relay a real fallback
// rather than a second attempt at the same URL.
const STREAMS=[
  {url:'http://cdn/1080.mp4', source_type:'http',protocol:'http',codec:'h264',height:1080,quality:'1080p',file:'fhd'},
  {url:'http://cdn/1080.m3u8',source_type:'hls', protocol:'hls', codec:'h264',height:1080,quality:'1080p',file:'fhd'},
];
const SEQ=7;
const fragError=(code)=>({type:'networkError',details:'fragLoadError',fatal:true,
  frag:{url:'/bridge/stream?url=seg42.ts',sn:42},response:{code:code||0}});
const mediaFatal=()=>({type:'mediaError',details:'bufferAppendError',fatal:true});

function fakeHls(){
  return {calls:[],startLoad(){this.calls.push('startLoad');},
    recoverMediaError(){this.calls.push('recoverMediaError');},
    swapAudioCodec(){this.calls.push('swapAudioCodec');},
    destroy(){this.calls.push('destroy');}};
}
function setup(){
  st.settings={quality:'auto',stream_mode:'auto',subtitles:'off',subtitle_size:100};
  st.mediaCaps=null;
  app.prepare({streams:STREAMS,subtitles:[],audios:[]},STREAMS[1]);
  st.mode='hls'; st.streamUrl='http://cdn/1080.m3u8'; st.episodeId='e1'; st.streamSwitchSeq=SEQ;
  st.audioHlsActive=false; st.audioHlsPreparing=false; st.audioHlsOffset=0;
  st.hlsFellBack=false; st.playerResumePosition=0; st.playerOriginalDuration=0;
  video.currentTime=600; video.paused=false;
  app.reset(); timers.clear(); reports.length=0; playerError.textContent='';
  return (st.hls=fakeHls());
}
const startLoads=h=>h.calls.filter(c=>c==='startLoad').length;

console.log('--- a fatal fragment error is recovered, not just printed ---');
let h=setup();
app.onError(fragError(502),SEQ);
check('the user is told it is being restored, not just that it broke',
  /восстанавливаем/i.test(playerError.textContent), true);
check('nothing is reloaded synchronously - the rung backs off first', h.calls, []);
runTimers();
check('hls.js is told to start loading again', h.calls, ['startLoad']);
check('the failure reaches the debug log with the status and the fragment',
  [reports[0].message,reports[0].details.code,reports[0].details.details,reports[0].details.sn],
  ['HLS fatal error',502,'fragLoadError',42]);
check('and with the position it died at, to match against the log',
  reports[0].details.position, 600);

console.log('--- a fragment that arrives resets the budget ---');
h=setup();
for(let i=0;i<3;i++){app.onError(fragError(),SEQ);runTimers();}
check('three failures used the three rungs', st.hlsNetworkRecoveries, 3);
app.progress(SEQ);
check('a buffered fragment puts the ladder back to the bottom', st.hlsNetworkRecoveries, 0);
check('and clears the recovery banner', playerError.textContent, '');

console.log('--- when HLS will not come back, relay takes over ---');
// Four fatal errors: three the ladder answers, the fourth exhausts it. (Each
// one already cost hls.js its own ~30 s of internal retrying, which is why the
// ladder is three rungs and not ten.)
h=setup();
for(let i=0;i<4;i++){app.onError(fragError(),SEQ);runTimers();}
check('it retried three times before giving up', startLoads(h), 3);
check('then reopened the same title as a progressive stream', st.mode, 'relay');
check('using the http variant, not the playlist again', st.streamUrl, 'http://cdn/1080.mp4');
check('and kept the position it had reached', Math.round(st.playerResumePosition), 600);
check('the switch is announced', /relay/i.test(playerError.textContent), true);

console.log('--- the fallback is one-shot ---');
for(let i=0;i<7;i++){app.onError(fragError(),st.streamSwitchSeq);runTimers();}
check('a stream that keeps failing after the fallback does not ping-pong', st.mode, 'relay');

console.log('--- a remuxed audio track is never traded for relay ---');
h=setup(); st.audioHlsActive=true;
for(let i=0;i<7;i++){app.onError(fragError(),SEQ);runTimers();}
check('the chosen track survives - no switch away from HLS', st.mode, 'hls');
check('and the failure is stated instead of hidden',
  /не восстановился/i.test(playerError.textContent), true);
check('with the position saved for a retry', Math.round(st.playerResumePosition), 600);

console.log('--- media errors get the decoder ladder, not the network one ---');
h=setup();
app.onError(mediaFatal(),SEQ);
check('the first one is recovered in place', h.calls, ['recoverMediaError']);
app.onError(mediaFatal(),SEQ);
check('the second swaps the audio codec first', h.calls,
  ['recoverMediaError','swapAudioCodec','recoverMediaError']);
check('no fragment reload is involved', startLoads(h), 0);
app.onError(mediaFatal(),SEQ);
check('the third hands over to relay', st.mode, 'relay');

console.log('--- a playlist that will not load is not retried at all ---');
h=setup();
app.onError({type:'networkError',details:'manifestLoadError',fatal:true,response:{code:403}},SEQ);
runTimers();
check('no point reloading a dead playlist URL', startLoads(h), 0);
check('it goes straight to relay', st.mode, 'relay');

console.log('--- noise is logged, not acted on ---');
h=setup();
app.onError({type:'networkError',details:'fragLoadError',fatal:false},SEQ);
runTimers();
check('a non-fatal error touches nothing',
  [h.calls,st.hlsNetworkRecoveries,playerError.textContent], [[],0,'']);
check('but is still visible in the log', [reports.length,reports[0].message], [1,'HLS error']);
// hls.js reports each of its own six retries, so this is what one incident
// looks like from here - it must leave one row, not six.
for(let i=0;i<20;i++)app.onError({type:'networkError',details:'fragLoadError',fatal:false},SEQ);
check('the rest of the burst does not bury the log', reports.length, 1);
check('a fatal error is never throttled away', (app.onError(fragError(500),SEQ),
  [reports.length,reports[1].message,reports[1].details.code]), [2,'HLS fatal error',500]);

console.log('--- errors from a stream that is already gone are ignored ---');
h=setup();
app.onError(fragError(),SEQ-1);
runTimers();
check('the previous stream cannot restart the current one',
  [h.calls,reports.length,st.hlsNetworkRecoveries], [[],0,0]);

console.log(failures ? `\n${failures} FAILED` : '\nAll green');
process.exit(failures ? 1 : 0);
