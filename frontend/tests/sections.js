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
  // Three subscribed serials, five unwatched episodes between them: the
  // badge counts episodes, not shows, so these two numbers must not be
  // interchangeable in the fixture or the test proves nothing.
  watchingList:()=>Promise.resolve({items:[{id:'w1',watching_new:2},{id:'w2',watching_new:0},{id:'w3',watching_new:3}]}),
  // The full watchlist is a different endpoint, not a slice of the one
  // above: a subscribed serial you have finished drops out of
  // v1/watching/serials entirely, so it can only come from the
  // history-backed assembly. Two extra finished shows here.
  subscribedSerials:()=>{ calls.push(['subscribedSerials']);
    return Promise.resolve({items:[{id:'w1',watching_new:2},{id:'w2',watching_new:0},{id:'w3',watching_new:3},
                                   {id:'w4',watching_new:0},{id:'w5',watching_new:0}],history_exhausted:true}); },
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
// The range sliders debounce their commit, so a filter move needs longer than
// one microtask turn to reach KPApi.catalog.
const settleRange = () => new Promise(r => setTimeout(r, 600));
const qa = s => [...document.querySelectorAll(s)];
const click = el => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
// jsdom ignores `keyCode` in the KeyboardEvent init dict, and every key path
// in app.js reads exactly that (webOS remotes report legacy key codes), so it
// has to be defined onto the event by hand.
const press = (el, code) => {
  const e = new window.KeyboardEvent('keydown', { bubbles: true, cancelable: true });
  Object.defineProperty(e, 'keyCode', { get: () => code });
  el.dispatchEvent(e);
  return e;
};
const OK = 13, LEFT = 37, RIGHT = 39, ESC = 27;
const bubbles = name => {
  const t = document.getElementById('filterRange_' + name);
  return t ? [t.querySelector('.range-bubble.lo').textContent,
              t.querySelector('.range-bubble.hi').textContent] : null;
};

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
  // Reported from the TV: one subscribed show with two unwatched episodes
  // showed "1" on the badge while its own card said "2 новые серии". The card
  // was right - `watching_new` is KinoPub's per-serial count of episodes not
  // yet seen, and the badge was counting list entries instead.
  check('counts new episodes, not serials, after the startup fetch', badge.textContent, '5');
  check('and is visible', badge.classList.contains('hidden'), false);

  // Reported from the TV: picking "ТВ Шоу" left the highlight sitting on
  // "Новинки". Cause was route() ending in focusFirst(), i.e. "focus the
  // first .focusable in the document" - and that is literally the "Новинки"
  // button, the first element in index.html. Cosmetic with a mouse, broken
  // with a remote: the ring is not where focus actually is, so the next Down
  // press moves from the wrong place.
  console.log('--- "Я смотрю": Новые эпизоды vs Все мои сериалы ---');
  calls.length = 0;
  app.route('watching'); await settleRange();
  const headText = () => document.querySelector('#catalogTop h3').textContent.replace(/\s+/g, ' ').trim();
  check('opens on the new-episodes view', headText(), 'Новые эпизоды 3');
  check('and asked the watching endpoint, not the watchlist one',
    calls.filter(c => c[0] === 'subscribedSerials').length, 0);
  const toggle = document.getElementById('watchingViewToggle');
  check('the kino.pub toggle is offered', toggle.textContent, 'Все мои сериалы');
  click(toggle); await settleRange();
  // Five, not three: the two finished subscriptions exist only in the
  // history-backed list. Shipping this as a filter over the watching payload
  // showed 2 of the user's 4 real subscriptions.
  check('the full list comes from its own endpoint', headText(), 'Мои сериалы 5');
  check('which was actually called', calls.filter(c => c[0] === 'subscribedSerials').length, 1);
  check('and the button offers the way back', document.getElementById('watchingViewToggle').textContent, 'Новые эпизоды');
  click(document.getElementById('watchingViewToggle')); await settleRange();
  check('back to new episodes', headText(), 'Новые эпизоды 3');

  console.log('--- navigating must not drag the focus ring to the first sidebar button ---');
  const sideLink = t => qa('.side-link').find(b => b.textContent.trim() === t);
  sideLink('ТВ Шоу').focus();
  click(sideLink('ТВ Шоу'));
  await settleRange();
  check('focus stays on the section that was picked',
    document.activeElement.textContent.trim(), 'ТВ Шоу');
  check('and the green marker is on the same one',
    document.querySelector('.side-link.active').textContent.trim(), 'ТВ Шоу');
  // Hash navigation, browser back/forward and first load arrive with nothing
  // focused; then the ring should land on where we ended up.
  document.activeElement.blur();
  app.route('anime'); await settleRange();
  check('with nothing focused it lands on the new section, not on "Новинки"',
    document.activeElement.textContent.trim(), 'Аниме');
  // "3D" has no sidebar button of its own - it only exists inside the
  // Фильмы/3D heading toggle, which renderTop() rebuilds on every
  // navigation, so the clicked node is detached by the time focus is placed.
  document.activeElement.blur();
  app.route('movie'); await settleRange();
  const inline3d = document.querySelector('[data-route-inline="3d"]');
  inline3d.focus(); click(inline3d); await settleRange();
  check('the 3D heading toggle keeps focus instead of falling back to the sidebar',
    document.activeElement.textContent.trim(), '3D');

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
  check('"Период" offers the real conditions[]=created>= presets',
    qa('#filterAdded option').map(o => o.value), ['','7','30','365']);

  console.log('--- kino.pub-style range sliders replace the year selects ---');
  check('three two-handle sliders', qa('.range-track').map(t => t.getAttribute('data-range')),
    ['year','kp','imdb']);
  check('the old year <select> pair is gone',
    [!!document.getElementById('filterYearFrom'), !!document.getElementById('filterYearTo')], [false,false]);
  // Whole numbers, not 0.1 steps: KinoPub discards the decimal part of a
  // rating bound (`imdb_rating>=7`, `>=7.5` and `>=7.9` all return the same
  // 8444 pages, verified live), so a handle reading 7.5 would be a lie.
  check('rating scales are integer ticks', qa('#filterRange_imdb .range-tick').map(t => t.textContent),
    ['0','2','4','6','8','10']);
  check('sliders start at their full extent', [bubbles('imdb'), bubbles('kp')], [['0','10'],['0','10']]);
  check('both kino.pub action buttons are there',
    qa('.filter-button').map(b => b.textContent), ['Сбросить','Мне повезёт!']);

  console.log('--- a slider is driven entirely by the remote ---');
  const imdb = document.getElementById('filterRange_imdb');
  imdb.focus();
  check('arrow keys pass through until edit mode is entered',
    imdb.classList.contains('editing'), false);
  press(imdb, OK);
  check('OK enters edit mode', imdb.classList.contains('editing'), true);
  check('and says how to drive it', /← → двигают левый край/.test(imdb.querySelector('.range-hint').textContent), true);
  press(imdb, OK);
  check('OK again swaps to the other handle',
    /← → двигают правый край/.test(imdb.querySelector('.range-hint').textContent), true);
  press(imdb, OK);
  calls.length = 0;
  for (let i = 0; i < 8; i++) press(imdb, RIGHT);
  check('right moves the low handle', bubbles('imdb')[0], '8');
  check('but has not refetched yet - the commit is debounced', calls.length, 0);
  await settleRange();
  check('one refetch for the whole burst, not one per keypress',
    calls.filter(c => c[0] === 'catalog').length, 1);
  check('with the moved bound', lastCatalogFilters.imdb_from, '8');
  check('and no upper bound, because that handle never moved', lastCatalogFilters.imdb_to, undefined);
  // Losing focus here would strand a remote user: the panel is rebuilt from
  // scratch on every commit, so it has to hand focus back to the same control.
  check('focus survives the rebuild', document.activeElement.id, 'filterRange_imdb');
  check('and so does edit mode',
    document.getElementById('filterRange_imdb').classList.contains('editing'), true);
  check('a moved range counts as one filter, not two',
    document.getElementById('filterToggle').textContent, 'Фильтры (1) ▾');

  press(document.getElementById('filterRange_imdb'), ESC);
  await settleRange();
  check('Back leaves edit mode', qa('.range-track.editing').length, 0);
  check('and the value stays put', bubbles('imdb'), ['8','10']);

  console.log('--- picking a filter actually refetches with it ---');
  calls.length = 0;
  const quality = document.getElementById('filterQuality');
  quality.value = '4';
  quality.dispatchEvent(new window.Event('change'));
  await settle();
  check('re-fetched the section', calls[0][0], 'catalog');
  check('with the chosen quality', lastCatalogFilters.quality, '4');
  check('panel stays open across the re-render', !!document.getElementById('filterPanel'), true);
  check('and keeps the chosen value', document.getElementById('filterQuality').value, '4');
  check('an active control is marked', document.getElementById('filterQuality').classList.contains('set'), true);

  console.log('--- resetting clears every filter in one click ---');
  click(document.getElementById('filterReset')); await settle();
  check('button label drops the count', document.getElementById('filterToggle').textContent, 'Фильтры ▾');
  check('select goes back to "any"', document.getElementById('filterQuality').value, '');
  check('and the sliders go back to their full extent', bubbles('imdb'), ['0','10']);

  console.log('--- switching sections keeps its own filter set separate ---');
  // The panel is still open from the earlier click (state.filterPanelOpen
  // is a single global flag, not per-section) - renderTop() re-shows it on
  // every route change, so no extra click needed here.
  document.getElementById('filterQuality').value = '2';
  document.getElementById('filterQuality').dispatchEvent(new window.Event('change'));
  await settle();
  app.route('serial'); await settle();
  check('a fresh section starts with no active filters',
    document.querySelector('#catalogTop .filter-toggle').textContent, 'Фильтры ▾');
  app.route('movie'); await settle();
  check('coming back to movies remembers its own filter',
    document.getElementById('filterQuality').value, '2');
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
