// History section, the 3D route, and the Фильмы/3D heading toggle, driven
// through the real app.js against the real index.html markup.
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

const DAY = 86400;
const now = Math.floor(Date.now() / 1000);
const HISTORY = {
  '': [
    { id:'1', type:'movie',  title:'Сегодняшний фильм', watched_at: now - 60 },
    { id:'2', type:'serial', title:'Ещё сегодня',       watched_at: now - 7200 },
    { id:'3', type:'movie',  title:'Вчерашний',         watched_at: now - DAY - 60 },
    { id:'4', type:'3d',     title:'Позавчера',         watched_at: now - 3 * DAY },
  ],
  movie: [{ id:'1', type:'movie', title:'Только фильм', watched_at: now - 60 }],
  '3d':  [{ id:'4', type:'3d',    title:'Только 3D',    watched_at: now - 3 * DAY }],
};

const GENRES = [{id:1,title:'Комедия'},{id:9,title:'Драма'}];
const COUNTRIES = [{id:1,title:'США'},{id:14,title:'Канада'}];
const FOLDERS = [{id:'f1',title:'йоу',count:5,views:3}];
const FOLDER_ITEMS = [
  {id:'m1',type:'movie',title:'Как стать миллионером'},
  {id:'m2',type:'movie',title:'Притворись моей женой'},
];

let lastCatalogFilters = null;
const calls = [];
global.KPApi = {
  status:()=>new Promise(()=>{}), settings:()=>new Promise(()=>{}), profile:()=>new Promise(()=>{}),
  report:()=>Promise.resolve(), watchingStatuses:()=>Promise.resolve({statuses:{}}),
  history:()=>Promise.resolve({items:[]}), hlsAudioVariants:()=>Promise.resolve({count:0}),
  createAudioHls:()=>new Promise(()=>{}), audioHlsStatus:()=>new Promise(()=>{}),
  stopAudioHls:()=>Promise.resolve({}), item:()=>new Promise(()=>{}),
  pageCount:()=>new Promise(()=>{}),
  catalog:(section,feed,page,nonce,perpage,filters)=>{ calls.push(['catalog',section,feed,page]);
    lastCatalogFilters = filters || {};
    return Promise.resolve({items:[],page:page||0,total_pages:3,total_items:0,has_next:true}); },
  kinoHistory:(page,type)=>{ calls.push(['history',page,type||'']);
    return Promise.resolve({items:HISTORY[type||'']||[],page:page||0,total_pages:2,total_items:4,has_next:true}); },
  genres:(type)=>{ calls.push(['genres',type]); return Promise.resolve({genres:GENRES}); },
  countries:()=>Promise.resolve({countries:COUNTRIES}),
  watchingList:()=>Promise.resolve({items:[{id:'w1'},{id:'w2'},{id:'w3'}]}),
  bookmarkFolders:()=>Promise.resolve({folders:FOLDERS}),
  bookmarkFolder:(id,page)=>{ calls.push(['bookmarkFolder',id,page]);
    return Promise.resolve({folder:{id,title:'йоу'},items:FOLDER_ITEMS,page:page||0,total_pages:1,total_items:5,has_next:false,perpage:48}); },
  imageProxyUrl:u=>u, streamProxyUrl:u=>u, hlsProxyUrl:u=>u, subtitleProxyUrl:u=>u,
};
window.KPApi = global.KPApi;

const src = fs.readFileSync(process.argv[2] || path.join(FRONT, 'app.js'), 'utf8');
eval(src.replace('}());', 'global.__app={state:state,route:route,renderCatalog:renderCatalog,'
  + 'setHistoryType:setHistoryType,routes:routes,dayLabel:historyDayLabel,'
  + 'loadWatchingCount:loadWatchingCount};}());'));
const app = global.__app, st = app.state;

let failures = 0;
const check = (n, got, want) => {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) failures++;
  console.log(`${ok?'PASS':'FAIL'}  ${n}\n        got ${JSON.stringify(got)}${ok?'':`\n        want ${JSON.stringify(want)}`}`);
};
const settle = () => new Promise(r => setTimeout(r, 20));
const qa = s => [...document.querySelectorAll(s)];
const click = el => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));

(async () => {
  console.log('--- 3D is its own section, reachable from the sidebar ---');
  check('3d route exists', !!app.routes['3d'], true);
  check('3d asks upstream for the 3d section', [app.routes['3d'].section, app.routes['3d'].feed], ['3d','all']);
  check('sidebar History button is wired',
    !!document.querySelector('[data-route="history"]'), true);

  calls.length = 0; app.route('3d'); await settle();
  check('opening 3D requests the 3d section', calls[0], ['catalog','3d','all',0]);

  console.log('--- Фильмы / 3D heading toggle ---');
  check('heading offers both', qa('.title-link').map(b => b.textContent), ['Фильмы','3D']);
  check('3D marked current while on 3D',
    qa('.title-link').map(b => b.classList.contains('title-current')), [false,true]);
  calls.length = 0; click(qa('[data-route-inline="movie"]')[0]); await settle();
  check('clicking Фильмы goes to the film list', calls[0], ['catalog','movie','all',0]);
  check('Фильмы marked current while on Фильмы',
    qa('.title-link').map(b => b.classList.contains('title-current')), [true,false]);
  // the second visit is served from the catalogue cache, so assert on the route
  click(qa('[data-route-inline="3d"]')[0]); await settle();
  check('clicking 3D goes to the 3D list, not back to films', st.route, '3d');
  check('and the heading follows',
    qa('.title-link').map(b => b.classList.contains('title-current')), [false,true]);

  console.log('--- History section ---');
  calls.length = 0; app.route('history'); await settle();
  check('requests page 0, all types', calls[0], ['history',0,'']);
  check('heading', document.querySelector('#catalogTop h3').textContent, 'История просмотров');
  check('type tabs', qa('.history-tabs .catalog-tab').map(b => b.textContent),
    ['Все','Фильмы','Сериалы','3D','Концерты','Докуфильмы','Докусериалы','ТВ Шоу']);
  check('grid switches to the day-grouped list',
    document.getElementById('catalogGrid').className, 'history-list');
  check('one heading per distinct day', qa('.history-day').map(h => h.textContent),
    ['Сегодня','Вчера', app.dayLabel(now - 3 * DAY)]);
  check('today groups both of its items',
    qa('.history-day')[0].nextElementSibling.querySelectorAll('.media-card').length, 2);
  check('cards render for every entry', qa('.media-card').length, 4);

  console.log('--- type filter ---');
  calls.length = 0; click(qa('.history-tabs .catalog-tab')[1]); await settle();
  check('Фильмы tab asks upstream for movie', calls[0], ['history',0,'movie']);
  check('active tab moved', document.querySelector('.history-tabs .catalog-tab.active').textContent, 'Фильмы');
  check('list replaced, not appended', qa('.media-card').length, 1);
  calls.length = 0; click(qa('.history-tabs .catalog-tab')[3]); await settle();
  check('3D tab asks upstream for 3d', calls[0], ['history',0,'3d']);

  console.log('--- paging is tracked per type ---');
  app.setHistoryType('');
  await settle();
  calls.length = 0;
  const next = qa('#catalogPagination .page-button').filter(b => b.textContent === '2')[0];
  check('pagination rendered', !!next, true);
  if (next) { click(next); await settle(); check('page 2 requested for the same filter', calls[0], ['history',1,'']); }

  console.log('--- leaving history restores the poster grid ---');
  calls.length = 0; app.route('movie'); await settle();
  check('grid class restored', document.getElementById('catalogGrid').className, 'poster-grid');

  console.log('--- "Я смотрю" sidebar badge shows a count before the section is ever opened ---');
  const badge = document.getElementById('watchingCount');
  check('starts blank/hidden, nothing fetched yet', badge.classList.contains('hidden'), true);
  await app.loadWatchingCount();
  check('shows the real count after the startup fetch', badge.textContent, '3');
  check('and is visible', badge.classList.contains('hidden'), false);

  console.log('--- filter panel: real options, not the old dead stub ---');
  app.route('movie'); await settle();
  check('closed by default', !!document.getElementById('filterPanel'), false);
  click(document.getElementById('filterToggle')); await settle();
  const panel = document.getElementById('filterPanel');
  check('opens on click', !!panel, true);
  check('genre options come from KPApi.genres, not a single "Любые" stub',
    qa('#filterGenre option').map(o => o.textContent), ['Любой','Комедия','Драма']);
  check('asks for genres scoped to this section\'s content type', calls.filter(c => c[0]==='genres').pop(), ['genres','movie']);
  check('country options come from KPApi.countries',
    qa('#filterCountry option').map(o => o.textContent), ['Любая','США','Канада']);
  check('quality options are the reference ids (4=4K), not raw resolutions',
    qa('#filterQuality option').map(o => o.value), ['','1','2','3','4']);
  check('sort has real v1/items field names', qa('#filterSort option').map(o => o.value)[1], '-created');
  check('year is a from/to range, not a single exact match',
    [!!document.getElementById('filterYearFrom'), !!document.getElementById('filterYearTo')], [true,true]);
  check('"Период" offers the real conditions[]=created>= presets',
    qa('#filterAdded option').map(o => o.value), ['','7','30','365']);

  console.log('--- picking a filter actually refetches with it ---');
  calls.length = 0;
  const yearFrom = document.getElementById('filterYearFrom');
  yearFrom.value = '2020';
  yearFrom.dispatchEvent(new window.Event('change'));
  await settle();
  check('re-fetched the section', calls[0][0], 'catalog');
  check('with the chosen year', lastCatalogFilters.year_from, '2020');
  check('toggle button shows an active-filter count', document.getElementById('filterToggle').textContent, 'Фильтры (1) ▾');
  check('panel stays open across the re-render', !!document.getElementById('filterPanel'), true);
  check('and keeps the chosen value', document.getElementById('filterYearFrom').value, '2020');

  console.log('--- resetting clears every filter in one click ---');
  click(document.getElementById('filterReset')); await settle();
  check('button label drops the count', document.getElementById('filterToggle').textContent, 'Фильтры ▾');
  check('select goes back to "any"', document.getElementById('filterYearFrom').value, '');

  console.log('--- switching sections keeps its own filter set separate ---');
  // The panel is still open from the earlier click (state.filterPanelOpen
  // is a single global flag, not per-section) - renderTop() re-shows it on
  // every route change, so no extra click needed here.
  document.getElementById('filterYearFrom').value = '2015';
  document.getElementById('filterYearFrom').dispatchEvent(new window.Event('change'));
  await settle();
  app.route('serial'); await settle();
  check('a fresh section starts with no active filters',
    document.querySelector('#catalogTop .filter-toggle').textContent, 'Фильтры ▾');
  app.route('movie'); await settle();
  check('coming back to movies remembers its own filter',
    document.getElementById('filterYearFrom').value, '2015');
  click(document.getElementById('filterReset')); await settle();
  click(document.getElementById('filterToggle')); await settle();
  check('panel now closed', !!document.getElementById('filterPanel'), false);

  console.log('--- Аниме: no genre picker, it would silently break the section ---');
  // "Аниме" is itself `genre=25` under the hood (see CATALOG_SECTIONS in
  // main.py), and v1/items only accepts one genre value - offering a
  // second genre choice here would silently replace "anime" with whatever
  // was picked. Confirmed live before this test existed: filtering Anime
  // by "Комедия" returned ordinary comedies, not anime comedies.
  calls.length = 0;
  app.route('anime'); await settle();
  click(document.getElementById('filterToggle')); await settle();
  check('genre select is not offered on this section', !!document.getElementById('filterGenre'), false);
  check('never asked KPApi.genres for it', calls.some(c => c[0] === 'genres'), false);
  check('country/year/quality/sort are still offered', !!document.getElementById('filterCountry'), true);
  click(document.getElementById('filterToggle')); await settle();
  app.route('movie'); await settle();
  click(document.getElementById('filterToggle')); await settle();
  check('genre select is back for a section that supports it', !!document.getElementById('filterGenre'), true);
  click(document.getElementById('filterToggle')); await settle();

  console.log('--- "Закладки": real folders, not a dead sidebar link ---');
  check('sidebar link is wired', !!document.querySelector('[data-route="bookmarks"]'), true);
  app.route('bookmarks'); await settle();
  check('heading', document.querySelector('#catalogTop h3').textContent, 'Закладки');
  check('one folder card for the one real folder',
    qa('.bookmark-folder-card .item-title').map(e => e.textContent), ['йоу']);
  check('shows the real item count', qa('.bookmark-folder-card .item-author')[0].textContent, '5 тайтлов');

  calls.length = 0;
  click(document.querySelector('.bookmark-folder-card')); await settle();
  check('opening a folder fetches its contents', calls[0], ['bookmarkFolder','f1',0]);
  check('renders the folder\'s real items, same card as everywhere else',
    qa('.media-card .item-title').map(e => e.textContent),
    ['Как стать миллионером','Притворись моей женой']);
  check('header names the open folder and offers a way back',
    document.querySelector('#catalogTop h3').textContent.indexOf('йоу') >= 0, true);
  check('a single-page folder shows no pagination bar at all',
    document.getElementById('catalogPagination').classList.contains('hidden'), true);

  click(document.getElementById('bookmarksBack')); await settle();
  check('back returns to the folder list', qa('.bookmark-folder-card').length, 1);

  console.log('--- leaving and returning to "Закладки" starts at the folder list again ---');
  app.route('bookmarks'); await settle();
  click(document.querySelector('.bookmark-folder-card')); await settle();
  app.route('movie'); await settle();
  app.route('bookmarks'); await settle();
  check('does not reopen the last folder', qa('.bookmark-folder-card').length, 1);

  console.log(failures ? `\n${failures} FAILURE(S)` : '\nAll checks passed');
  process.exit(failures ? 1 : 0);
})();
