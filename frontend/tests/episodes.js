// Season/episode selector visibility: movies (single file) must show neither
// a season picker nor an episode strip; a flat multi-episode title with no
// real season data must show episodes but no season pills; a real multi-season
// title keeps the season picker.
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

global.KPApi = {
  status:()=>new Promise(()=>{}), settings:()=>new Promise(()=>{}), profile:()=>new Promise(()=>{}),
  report:()=>Promise.resolve(), watchingStatuses:()=>Promise.resolve({statuses:{}}),
  hlsAudioVariants:()=>Promise.resolve({count:0}), createAudioHls:()=>new Promise(()=>{}),
  audioHlsStatus:()=>new Promise(()=>{}), stopAudioHls:()=>Promise.resolve({}),
  history:()=>Promise.resolve({items:[]}), saveProgress:()=>Promise.resolve({}),
  imageProxyUrl:u=>u, streamProxyUrl:u=>u, hlsProxyUrl:u=>u, subtitleProxyUrl:u=>u,
};
window.KPApi = global.KPApi;

const src = fs.readFileSync(process.argv[2] || path.join(FRONT, 'app.js'), 'utf8');
eval(src.replace('}());', 'global.__app={state:state,renderEpisodes:renderDetailsEpisodes,'
  + 'episodeLabel:episodeLabel,hasMultipleSeasons:hasMultipleSeasons};}());'));
const app = global.__app;

let failures = 0;
const check = (n, got, want) => {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) failures++;
  console.log(`${ok?'PASS':'FAIL'}  ${n}\n        got ${JSON.stringify(got)}${ok?'':`\n        want ${JSON.stringify(want)}`}`);
};
const qa = s => [...document.querySelectorAll(s)];

console.log('--- movie: a single media entry, backend\'s default one-season grouping ---');
const MOVIE = {
  id: 'm1', title: 'Дюна', poster: '', backdrop: '',
  media: [{ id: 'f1', title: 'Дюна', season: null, episode: null }],
  seasons: [{ number: 1, episodes: [{ id: 'f1', title: 'Дюна', season: null, episode: null }] }],
};
app.renderEpisodes(MOVIE);
check('no season pills', qa('.season-pill').length, 0);
check('no episode strip', qa('.episode-card').length, 0);
check('nothing at all rendered', document.getElementById('detailsEpisodes').innerHTML, '');
check('hasMultipleSeasons is false', app.hasMultipleSeasons(MOVIE), false);

console.log('--- movie with a genuinely empty seasons array (no media found) ---');
const EMPTY = { id: 'm2', title: 'X', media: [], seasons: [] };
app.renderEpisodes(EMPTY);
check('still nothing rendered', document.getElementById('detailsEpisodes').innerHTML, '');

console.log('--- flat miniseries: one synthesized season, several episodes ---');
const FLAT = {
  id: 's1', title: 'Мини-сериал', poster: '', backdrop: '',
  media: [
    { id: 'e1', title: 'Часть 1', season: null, episode: 1 },
    { id: 'e2', title: 'Часть 2', season: null, episode: 2 },
    { id: 'e3', title: 'Часть 3', season: null, episode: 3 },
  ],
  seasons: [{ number: 1, episodes: [
    { id: 'e1', title: 'Часть 1', season: null, episode: 1 },
    { id: 'e2', title: 'Часть 2', season: null, episode: 2 },
    { id: 'e3', title: 'Часть 3', season: null, episode: 3 },
  ]}],
};
app.renderEpisodes(FLAT);
check('no season pills for a single-season grouping', qa('.season-pill').length, 0);
check('flat episode strip has all three', qa('.episode-card').length, 3);
check('episode cards carry no SxxEyy code (no real season data)',
  qa('.episode-code').length, 0);
check('episode titles present', qa('.episode-name').map(e => e.textContent), ['Часть 1','Часть 2','Часть 3']);
check('resume label falls back to the plain title',
  app.episodeLabel(FLAT, FLAT.media[1]), 'Часть 2');

console.log('--- real multi-season series keeps the season picker ---');
const SERIES = {
  id: 's2', title: 'Сериал', poster: '', backdrop: '',
  seasons: [
    { number: 1, episodes: [{ id: 'a1', title: 'A1', season: 1, episode: 1 }] },
    { number: 2, episodes: [{ id: 'b1', title: 'B1', season: 2, episode: 1 }] },
  ],
};
app.renderEpisodes(SERIES);
check('season pills shown', qa('.season-pill').map(p => p.textContent), ['1','2']);
check('first season strip has its episode', qa('.episode-card').length, 1);
check('SxxEyy code shown for a real multi-season title',
  qa('.episode-code').map(e => e.textContent), ['S01E01']);
check('resume label includes the SxxEyy prefix',
  app.episodeLabel(SERIES, SERIES.seasons[1].episodes[0]), 'S02E01 · B1');

console.log(failures ? `\n${failures} FAILURE(S)` : '\nAll checks passed');
process.exit(failures ? 1 : 0);
