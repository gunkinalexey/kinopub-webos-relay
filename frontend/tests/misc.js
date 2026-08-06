// Regressions for the audit pass: timeline OK-key handling and the shared
// track-label helpers extracted from the two duplicated closures.
const fs = require('fs');

function makeEl(id) {
  const el = {
    id, textContent: '', value: '', disabled: false, selectedIndex: 0,
    children: [], dataset: {}, style: {}, offsetParent: {}, attrs: {},
    classList: { _s:new Set(), add(...c){c.forEach(x=>this._s.add(x));}, remove(...c){c.forEach(x=>this._s.delete(x));}, toggle(c,o){o?this._s.add(c):this._s.delete(c);}, contains(c){return this._s.has(c);} },
    appendChild(c){this.children.push(c);c.parentNode=this;return c;},
    removeChild(c){const i=this.children.indexOf(c);if(i>=0)this.children.splice(i,1);return c;},
    remove(){if(this.parentNode)this.parentNode.removeChild(this);},
    querySelector(){return null;}, querySelectorAll(){return [];},
    setAttribute(k,v){this.attrs[k]=v;}, getAttribute(k){return this.attrs[k]||null;},
    focus(){}, click(){}, scrollIntoView(){}, addEventListener(){}, removeEventListener(){},
    // 5vw left padding on the player UI, as in the real layout
    getBoundingClientRect(){return {left:96,top:900,width:1728,height:8};},
    contains(){return false;}, load(){}, pause(){}, play(){return Promise.resolve();},
    removeAttribute(){},
  };
  let html='';
  Object.defineProperty(el,'innerHTML',{get:()=>html,set(v){html=v;el.children.length=0;}});
  return el;
}
const els={}, $$=id=>(els[id]||(els[id]=makeEl(id)));
const video=$$('video');
Object.assign(video,{paused:false,ended:false,currentTime:600,duration:7200,readyState:2,
  error:null,textTracks:[],audioTracks:[],seekable:{length:1,start:()=>0,end:()=>7200},
  // model real play/pause state so toggling is observable
  pause(){this.paused=true;},
  play(){this.paused=false;return global.PLAY_RESULT||Promise.resolve();}});
global.window={KP_BACKEND:'/bridge'}; global.screen={width:1920,height:1080};
global.navigator={userAgent:'test'}; global.sessionStorage={clear(){}}; global.localStorage={removeItem(){}};
global.document={getElementById:$$,createElement:t=>{const e=makeEl('new-'+t);e.tagName=t.toUpperCase();return e;},
  querySelectorAll:()=>[],querySelector:()=>null,addEventListener(){},head:makeEl('head'),body:makeEl('body'),
  activeElement:null,title:''};
global.Hls=undefined;
global.KPApi={status:()=>new Promise(()=>{}),settings:()=>new Promise(()=>{}),profile:()=>new Promise(()=>{}),
  report:()=>Promise.resolve(),watchingStatuses:()=>Promise.resolve({statuses:{}}),history:()=>Promise.resolve({items:[]}),
  hlsAudioVariants:()=>Promise.resolve({count:0}),createAudioHls:()=>new Promise(()=>{}),
  audioHlsStatus:()=>new Promise(()=>{}),stopAudioHls:()=>Promise.resolve({}),saveProgress:()=>Promise.resolve({}),
  streamProxyUrl:u=>u,hlsProxyUrl:u=>u,imageProxyUrl:u=>u,subtitleProxyUrl:u=>u};

const src=fs.readFileSync(process.argv[2],'utf8');
eval(src.replace('}());','global.__app={state:state,seekFrom:seekFromTimelineEvent,'
  +'audioLabel:detailedAudioLabel,subLabel:detailedSubtitleLabel,'
  +'pushPart:pushLabelPart,flag:truthyFlag,start:startPlayback,'
  +'mediaError:mediaError};}());'));
const app=global.__app, st=app.state;

let failures=0;
const check=(n,got,want)=>{const ok=JSON.stringify(got)===JSON.stringify(want);if(!ok)failures++;
  console.log(`${ok?'PASS':'FAIL'}  ${n}\n        got ${JSON.stringify(got)}${ok?'':`\n        want ${JSON.stringify(want)}`}`);};

console.log('--- timeline: remote OK key must not rewind to 00:00 ---');
st.playerOriginalDuration=7200; st.audioHlsActive=false; video.currentTime=600; video.paused=false;
// activeElement.click() produces detail === 0 and clientX === 0
app.seekFrom({detail:0,clientX:0});
check('OK key does not seek', Math.round(video.currentTime), 600);
check('OK key toggles playback instead', video.paused, true);

console.log('--- timeline: a real pointer click still seeks ---');
video.paused=false; video.currentTime=600;
// halfway along a bar spanning x=96..1824
app.seekFrom({detail:1,clientX:96+864});
check('mouse click seeks to the midpoint', Math.round(video.currentTime), 3600);
check('and does not toggle playback', video.paused, false);

console.log('--- shared label helpers behave as the two closures did ---');
const parts=[];
app.pushPart(parts,'Русский'); app.pushPart(parts,'русский');   // case-insensitive dupe
app.pushPart(parts,''); app.pushPart(parts,null); app.pushPart(parts,{});
app.pushPart(parts,'LostFilm');
check('dedupes case-insensitively and drops empties', parts, ['Русский','LostFilm']);

check('truthyFlag(true)',   app.flag(true),   true);
check('truthyFlag("yes")',  app.flag('yes'),  true);
check('truthyFlag("1")',    app.flag('1'),    true);
check('truthyFlag(1)',      app.flag(1),      true);
check('truthyFlag(false)',  app.flag(false),  false);
check('truthyFlag("")',     app.flag(''),     false);
check('truthyFlag(null)',   app.flag(null),   false);

check('audio label unchanged',
  app.audioLabel({lang:'rus',author:{title:'LostFilm'},channels:6,codec:'eac3'},0,false),
  '1. Русский · LostFilm · 5.1 · EAC3');
check('audio label falls back when empty',
  app.audioLabel({},2,false), '3. Дорожка 3');
check('subtitle label with flags',
  app.subLabel({lang:'rus',forced:true,hearing_impaired:'yes',format:'subrip'},0,false),
  '1. Русский · форсированные · SDH · SRT');
check('subtitle label falls back when empty',
  app.subLabel({},1,false), '2. Субтитры 2');

console.log('--- play() rejections ---');
const mkerr = (name, message) => Object.assign(new Error(message), { name });
const settle = () => new Promise(r => setTimeout(r, 10));

(async () => {
  st.streamSwitchSeq = 5;
  st.playerResumePosition = 0;

  // The exact rejection the user reported.
  $$("playerError").textContent = '';
  global.PLAY_RESULT = Promise.reject(mkerr('AbortError',
    'The play() request was interrupted by a call to pause(). https://goo.gl/LdLk22'));
  app.start(5); await settle();
  check('AbortError from pause() stays silent', $$("playerError").textContent, '');

  // load() during a stream switch aborts play the same way.
  $$("playerError").textContent = '';
  global.PLAY_RESULT = Promise.reject(mkerr('AbortError',
    'The play() request was interrupted by a new load request.'));
  app.start(5); await settle();
  check('AbortError from load() stays silent', $$("playerError").textContent, '');

  // A rejection belonging to a switch we already navigated away from.
  $$("playerError").textContent = '';
  global.PLAY_RESULT = Promise.reject(mkerr('NotSupportedError', 'boom'));
  app.start(4); await settle();
  check('stale switch rejection stays silent', $$("playerError").textContent, '');

  // Autoplay policy gets its own actionable message, not a media error.
  $$("playerError").textContent = '';
  global.PLAY_RESULT = Promise.reject(mkerr('NotAllowedError', 'autoplay blocked'));
  app.start(5); await settle();
  check('NotAllowedError asks the user to press OK',
    /автозапуск/i.test($$("playerError").textContent), true);

  // A genuine failure is still surfaced.
  $$("playerError").textContent = '';
  global.PLAY_RESULT = Promise.reject(mkerr('NotSupportedError', 'codec not supported'));
  app.start(5); await settle();
  check('real failure still reported',
    $$("playerError").textContent.indexOf('codec not supported') === 0, true);

  global.PLAY_RESULT = null;

  console.log('--- position notice ---');
  st.playerResumePosition = 0; st.playerOriginalDuration = 0;
  video.currentTime = 0; st.audioHlsActive = false;
  app.mediaError('Сетевая ошибка.');
  check('no position clause at 00:00', $$("playerError").textContent, 'Сетевая ошибка.');

  st.playerResumePosition = 754;
  app.mediaError('Сетевая ошибка.');
  check('position clause when there is a position',
    $$("playerError").textContent, 'Сетевая ошибка. Позиция 12:34 сохранена.');

  console.log(failures ? '\n' + failures + ' FAILURE(S)' : '\nAll checks passed');
  process.exit(failures ? 1 : 0);
})();
