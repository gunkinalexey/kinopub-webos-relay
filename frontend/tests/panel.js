// Runs the real renderDetails against the real index.html markup in a real DOM,
// then writes a standalone preview page (tests/preview.html) using the real
// stylesheet — open it in a browser to eyeball the layout.
//
// Usage: node panel.js [path/to/app.js] [path/to/frontend/dir]
// Defaults assume this file lives in frontend/tests/.
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const FRONT = process.argv[3] || path.join(__dirname, '..');
const APP_JS = process.argv[2] || path.join(FRONT, 'app.js');
const html = fs.readFileSync(path.join(FRONT, 'index.html'), 'utf8');
const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true });
const { window } = dom;

global.window = window;
global.document = window.document;
global.screen = window.screen;
global.navigator = window.navigator;
global.sessionStorage = { clear() {} };
global.localStorage = { removeItem() {} };
global.Hls = undefined;
global.setInterval = () => 0;
global.clearInterval = () => {};

const ITEM = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixture.json'), 'utf8'));

let similarCalls = [];
let similarAnswer = () => Promise.resolve({ items: [] });
global.KPApi = {
  status: () => new Promise(() => {}), settings: () => new Promise(() => {}),
  profile: () => new Promise(() => {}), report: () => Promise.resolve(),
  watchingStatuses: () => Promise.resolve({ statuses: {} }),
  hlsAudioVariants: () => Promise.resolve({ count: 0 }), createAudioHls: () => new Promise(() => {}),
  audioHlsStatus: () => new Promise(() => {}), stopAudioHls: () => Promise.resolve({}),
  item: () => Promise.resolve(ITEM),
  // Similar titles: what the stub answers is switched per test, because both
  // answers matter - a populated one and the empty one that most of the
  // catalogue actually returns.
  similar: (id) => { similarCalls.push(id); return similarAnswer(); },
  history: () => Promise.resolve({items:[{media_id:'77035',episode_id:'813841',position:754,duration:2640,completed:0,updated_at:200}]}),
  imageProxyUrl: u => u, streamProxyUrl: u => u, hlsProxyUrl: u => u, subtitleProxyUrl: u => u,
};
window.KPApi = global.KPApi;

const src = fs.readFileSync(APP_JS, 'utf8');
eval(src.replace('}());', 'global.__app={renderDetails:renderDetails,openDetails:openDetails,'
  + 'closeDetails:closeDetails,showScreen:showScreen,visibleScreen:visibleScreen,route:route,'
  + 'loadProgress:loadItemProgress,renderActions:renderDetailsActions,'
  + 'loadSimilar:loadSimilar,state:state};}());'));

(async () => {
global.__app.renderDetails(ITEM);
// The resume buttons only exist once the saved position has arrived.
await global.__app.loadProgress(ITEM);
global.__app.renderActions(ITEM);

const modal = document.getElementById('detailsScreen');
const css = fs.readFileSync(path.join(FRONT, 'styles.css'), 'utf8');
const sprite = html.slice(html.indexOf('<svg class="icon-sprite"'), html.indexOf('</svg>') + 6);

fs.writeFileSync(path.join(__dirname, 'preview.html'),
`<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>details preview</title>
<style>${css}</style></head><body>${sprite}<div class="app-shell"><aside class="sidebar"></aside><section class="content-shell">${modal.outerHTML}</section></div></body></html>`);

// Structural assertions on what actually rendered
const q = s => modal.querySelector(s);
const qa = s => modal.querySelectorAll(s);
let failures = 0;
const check = (name, got, want) => {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) failures++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}\n        got ${JSON.stringify(got)}${ok ? '' : `\n        want ${JSON.stringify(want)}`}`);
};

check('title', q('#detailsTitle').textContent, 'Тед Лассо');
check('original + year', q('#detailsOriginal').textContent, 'Ted Lasso · 2020');
check('vote counters', [q('.details-vote-up').textContent.trim(), q('.details-vote-down').textContent.trim()],
  ['▲ 2834', '▼ 103']);
check('tabs', [...qa('.details-tab')].map(t => t.textContent), ['Сюжет', 'Аудио', 'Субтитры']);
check('plot tab active first', q('.details-tab.active').textContent, 'Сюжет');
check('plot body rendered', q('#detailsTabBody').textContent.slice(0, 22), 'Американский тренер по');

const keys = [...qa('.details-info-key')].map(k => k.textContent);
check('info rows', keys,
  ['Рейтинг','Всего','Статус','Год выхода','Страна','Жанр','Режиссёр','В ролях','Длительность','Качество','Субтитры']);
const val = i => qa('.details-info-value')[keys.indexOf(i)].textContent.replace(/\s+/g, ' ').trim();
check('rating cell', val('Рейтинг'), 'КП 8.5 / 142 415 IMDb 8.7 / 453 363 KinoPub 9.6');
check('totals pluralised', val('Всего'), '4 сезона и 36 эпизодов');
check('ongoing status', val('Статус'), 'Выходит');
check('countries', val('Страна'), 'США, Великобритания');
check('duration', val('Длительность'), '45:00 / 45 мин');

check('season pills', [...qa('.season-pill')].map(p => p.textContent), ['1','2','3','4']);
check('first season active', q('.season-pill.active').textContent, '1');
check('episodes of season 1', qa('.episode-card').length, 2);
check('episode badge', q('.episode-badge').textContent, 'Эпизод 1');
check('episode code', q('.episode-code').textContent, 'S01E01');
check('episode title', q('.episode-name').textContent, 'Пилот');

// switching season must swap the strip, not append to it
[...qa('.season-pill')][1].dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
check('season 2 active after click', q('.season-pill.active').textContent, '2');
check('strip replaced, not appended', qa('.episode-card').length, 1);
check('season 2 code', q('.episode-code').textContent, 'S02E01');

// switching tab must swap the body
[...qa('.details-tab')][1].dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
check('audio tab active', q('.details-tab.active').textContent, 'Аудио');
check('audio list rendered', [...qa('#detailsTabBody li')].map(l => l.textContent),
  ['Русский · Дубляж · LostFilm · 5.1 · EAC3', 'Английский · Оригинал · 2.0 · AAC']);

console.log('--- details is a screen, not an overlay ---');
const app2 = global.__app;
const vis = id => !document.getElementById(id).classList.contains('hidden');

app2.showScreen('catalogScreen');
check('catalog visible to start', [vis('catalogScreen'), vis('detailsScreen')], [true, false]);

app2.state.detailsReturn = 'catalogScreen';
app2.showScreen('detailsScreen');
check('details replaces the grid rather than covering it',
  [vis('catalogScreen'), vis('searchScreen'), vis('settingsScreen'), vis('detailsScreen')],
  [false, false, false, true]);

app2.closeDetails();
check('Back returns to the catalogue', [vis('catalogScreen'), vis('detailsScreen')], [true, false]);

// coming from search must go back to search, not to the catalogue
app2.showScreen('searchScreen');
app2.state.detailsReturn = app2.visibleScreen();
app2.showScreen('detailsScreen');
app2.closeDetails();
check('Back from a search result returns to search',
  [vis('searchScreen'), vis('catalogScreen'), vis('detailsScreen')], [true, false, false]);

// sidebar navigation while details is open must leave details behind
app2.showScreen('detailsScreen');
app2.showScreen('settingsScreen');
check('switching screens hides details', [vis('settingsScreen'), vis('detailsScreen')], [true, false]);

check('only ever one screen visible',
  ['catalogScreen','searchScreen','settingsScreen','detailsScreen'].filter(vis).length, 1);

// "Похожие" is KinoPub's own recommendation list (v1/items/similar). Only
// about a third of the catalogue has one at all - measured live, fresh
// serials 1 in 15 - so the section has to disappear rather than stand there
// empty, and no genre-based stand-in is invented to fill it.
console.log('--- "Похожие" ---');
const similarBlock = () => document.getElementById('detailsSimilarBlock');
const similarCards = () => [...document.querySelectorAll('#detailsSimilar .media-card')];

similarCalls = [];
similarAnswer = () => Promise.resolve({ items: [] });
global.__app.loadSimilar({ id: '82354', title: 'Дом дракона' });
await new Promise(r => setTimeout(r, 20));
check('asked KinoPub for this title', similarCalls, ['82354']);
check('an empty answer hides the section entirely', similarBlock().classList.contains('hidden'), true);
check('and leaves no cards behind', similarCards().length, 0);

similarAnswer = () => Promise.resolve({ items: [
  { id: '91', title: 'Властелин колец', poster: '', year: 2001 },
  { id: '92', title: 'Хоббит', poster: '', year: 2012 },
]});
global.__app.loadSimilar({ id: '8740', title: 'Игра престолов' });
await new Promise(r => setTimeout(r, 20));
check('a populated answer shows the section', similarBlock().classList.contains('hidden'), false);
check('one card per similar title',
  similarCards().map(c => c.querySelector('.item-title').textContent), ['Властелин колец','Хоббит']);

// Opening a second title before the first answer arrives must not drop that
// answer under the title now on screen.
let release;
similarAnswer = () => new Promise(r => { release = r; });
global.__app.loadSimilar({ id: 'slow', title: 'Медленный' });
similarAnswer = () => Promise.resolve({ items: [{ id: '99', title: 'Актуальный', poster: '' }] });
global.__app.loadSimilar({ id: 'fast', title: 'Быстрый' });
await new Promise(r => setTimeout(r, 20));
release({ items: [{ id: '1', title: 'Устаревший', poster: '' }] });
await new Promise(r => setTimeout(r, 20));
check('a late answer for the previous title is discarded',
  similarCards().map(c => c.querySelector('.item-title').textContent), ['Актуальный']);

console.log(failures ? `\n${failures} FAILURE(S)` : '\nAll checks passed');
process.exit(failures ? 1 : 0);
})();
