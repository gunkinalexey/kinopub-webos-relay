// Details screen: watch/continue actions and where the episode strip sits.
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const FRONT = process.argv[3] || path.join(__dirname, '..');
const html = fs.readFileSync(path.join(FRONT, 'index.html'), 'utf8');
const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true });
const { window } = dom;
global.window = window; global.document = window.document;
global.screen = window.screen; global.navigator = window.navigator;
global.sessionStorage = { clear(){} }; global.localStorage = { removeItem(){} };
global.Hls = undefined; global.setInterval = () => 0; global.clearInterval = () => {};

const ITEM = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixture.json'), 'utf8'));
let PROGRESS = { items: [] };
const played = [];

global.KPApi = {
  status:()=>new Promise(()=>{}), settings:()=>new Promise(()=>{}), profile:()=>new Promise(()=>{}),
  report:()=>Promise.resolve(), watchingStatuses:()=>Promise.resolve({statuses:{}}),
  hlsAudioVariants:()=>Promise.resolve({count:0}), createAudioHls:()=>new Promise(()=>{}),
  audioHlsStatus:()=>new Promise(()=>{}), stopAudioHls:()=>Promise.resolve({}),
  item:()=>Promise.resolve(ITEM), saveProgress:()=>Promise.resolve({}),
  history:(mediaId)=>Promise.resolve(mediaId ? PROGRESS : {items:[]}),
  play:(id, mediaId)=>{ played.push(['resolve', id, mediaId||null]); return new Promise(()=>{}); },
  imageProxyUrl:u=>u, streamProxyUrl:u=>u, hlsProxyUrl:u=>u, subtitleProxyUrl:u=>u,
};
window.KPApi = global.KPApi;

const src = fs.readFileSync(process.argv[2] || path.join(FRONT, 'app.js'), 'utf8');
eval(src.replace('}());', 'global.__app={state:state,renderDetails:renderDetails,openDetails:openDetails,'
  + 'renderActions:renderDetailsActions,loadProgress:loadItemProgress};}());'));
const app = global.__app, st = app.state;

let failures = 0;
const check = (n, got, want) => {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) failures++;
  console.log(`${ok?'PASS':'FAIL'}  ${n}\n        got ${JSON.stringify(got)}${ok?'':`\n        want ${JSON.stringify(want)}`}`);
};
const settle = () => new Promise(r => setTimeout(r, 20));
const qa = s => [...document.querySelectorAll(s)];
const labels = () => qa('#detailsActions button').map(b => b.textContent);
const click = el => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));

(async () => {
  console.log('--- layout: actions and episodes sit above the poster/info grid ---');
  st.playerResumePosition = 0;
  PROGRESS = { items: [] };
  app.renderDetails(ITEM); await settle();

  const body = document.querySelector('#detailsScreen .details-body');
  const order = [...body.children].map(c => c.id || c.className);
  check('order under the headline', order,
    ['detailsActions', 'detailsEpisodes', 'details-grid']);
  // Vote buttons legitimately live in the aside (next to the poster) - the
  // invariant this guards is that Watch/Continue never drifts back in there.
  check('poster column no longer holds a play button',
    document.querySelectorAll('.details-aside .details-play, .details-aside .details-play-secondary').length, 0);

  console.log('--- never watched: one plain button ---');
  check('single Watch button', labels(), ['▶ Смотреть']);

  console.log('--- watched half way: continue + restart ---');
  PROGRESS = { items: [
    { media_id: '77035', episode_id: '813841', position: 754, duration: 2640, completed: 0, updated_at: 200 },
    { media_id: '77035', episode_id: '813082', position: 120, duration: 2700, completed: 1, updated_at: 100 },
  ]};
  await app.loadProgress(ITEM); await settle();
  check('two buttons', labels(), ['▶ Продолжить 12:34', 'Начать заново']);
  check('names the episode being continued',
    document.querySelector('.details-resume-note').textContent, 'S01E02 · Ставки');

  console.log('--- continue starts that episode at the saved point ---');
  played.length = 0; st.playerResumePosition = 0;
  click(qa('#detailsActions button')[0]); await settle();
  check('resume position applied', Math.round(st.playerResumePosition), 754);
  check('resolved the right episode', played[0], ['resolve', '77035', '813841']);

  console.log('--- restart plays the same episode from zero ---');
  played.length = 0; st.playerResumePosition = 999;
  click(qa('#detailsActions button')[1]); await settle();
  check('position reset', st.playerResumePosition, 0);
  check('same episode', played[0], ['resolve', '77035', '813841']);

  console.log('--- what does NOT count as resumable ---');
  PROGRESS = { items: [{ media_id:'77035', episode_id:'813082', position: 2600, duration: 2700, completed: 1, updated_at: 1 }] };
  await app.loadProgress(ITEM); await settle();
  check('finished title offers only Watch', labels(), ['▶ Смотреть']);

  PROGRESS = { items: [{ media_id:'77035', episode_id:'813082', position: 8, duration: 2700, completed: 0, updated_at: 1 }] };
  await app.loadProgress(ITEM); await settle();
  check('barely started offers only Watch', labels(), ['▶ Смотреть']);

  PROGRESS = { items: [{ media_id:'77035', episode_id:'813082', position: 2695, duration: 2700, completed: 0, updated_at: 1 }] };
  await app.loadProgress(ITEM); await settle();
  check('a few seconds from the end offers only Watch', labels(), ['▶ Смотреть']);

  console.log('--- a fresh title must not inherit the previous position ---');
  PROGRESS = { items: [] };
  st.playerResumePosition = 4321;
  await app.loadProgress(ITEM); await settle();
  played.length = 0;
  click(qa('#detailsActions button')[0]); await settle();
  check('starts at zero, not the previous title\'s time', st.playerResumePosition, 0);

  console.log(failures ? `\n${failures} FAILURE(S)` : '\nAll checks passed');
  process.exit(failures ? 1 : 0);
})();
