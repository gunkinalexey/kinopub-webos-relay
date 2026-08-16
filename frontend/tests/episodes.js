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
  // Echoes the requested media id back as `media.id`, same as the real
  // backend does for a specific media_id request - lets the next-episode
  // flow test below drive the actual play() promise chain instead of
  // poking state by hand.
  play:(itemId,mediaId)=>Promise.resolve({media:{id:mediaId||'a1'},
    streams:[{url:'http://cdn/ep.mp4',source_type:'http',protocol:'http',codec:'h264',height:1080,quality:'1080p',file:'f'}],
    selected:null,subtitles:[],audios:[],expected_tracks:0}),
};
window.KPApi = global.KPApi;

const src = fs.readFileSync(process.argv[2] || path.join(FRONT, 'app.js'), 'utf8');
eval(src.replace('}());', 'global.__app={state:state,renderEpisodes:renderDetailsEpisodes,'
  + 'episodeLabel:episodeLabel,hasMultipleSeasons:hasMultipleSeasons,'
  + 'nextEpisode:nextEpisodeEntry,updateNextButton:updateNextEpisodeButton,'
  + 'autoplay:autoplayNextEpisode,play:play};}());'));
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
// One numbered button per episode, the same control as the season picker.
// Its own class, not `.season-pill` - they look alike but a query for season
// pills must not pick up episodes.
check('an episode pill per episode', qa('.episode-pill').map(p => p.textContent), ['1','2','3']);
check('episode pills are not season pills', qa('.season-pill').length, 0);
check('nothing is marked watched without any progress data',
  qa('.episode-pill.watched').length, 0);

console.log('--- watched and part-watched episodes are marked differently ---');
// e1 finished, e2 stopped in the middle, e3 never opened. "Watched" and
// "resume this one" are genuinely different states; collapsing them would
// hide exactly the episode the user stopped in.
app.state.detailsProgress = { items: [
  { episode_id: 'e1', position: 1200, duration: 1200, completed: true },
  { episode_id: 'e2', position: 400,  duration: 1200, completed: false },
]};
app.renderEpisodes(FLAT);
check('finished episode is filled',
  qa('.episode-pill').map(p => p.classList.contains('watched')), [true,false,false]);
check('part-watched episode is outlined instead',
  qa('.episode-pill').map(p => p.classList.contains('partial')), [false,true,false]);
app.state.detailsProgress = null;

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
check('episode pills follow the shown season, not the whole series',
  qa('.episode-pill').map(p => p.textContent), ['1']);
check('SxxEyy code shown for a real multi-season title',
  qa('.episode-code').map(e => e.textContent), ['S01E01']);
check('resume label includes the SxxEyy prefix',
  app.episodeLabel(SERIES, SERIES.seasons[1].episodes[0]), 'S02E01 · B1');

console.log('--- season pills carry the same watched/partial signal as episode pills ---');
// Season 1: one finished, one stopped mid-way, one never opened - "some
// progress, not all of it" is exactly what episode-partial means one level up.
// Season 2: everything finished. Season 3: nothing touched at all.
const SEASONED = {
  id: 's3', title: 'Другой сериал', poster: '', backdrop: '',
  seasons: [
    { number: 1, episodes: [
      { id: 'p1', title: 'P1', season: 1, episode: 1 },
      { id: 'p2', title: 'P2', season: 1, episode: 2 },
      { id: 'p3', title: 'P3', season: 1, episode: 3 },
    ]},
    { number: 2, episodes: [
      { id: 'p4', title: 'P4', season: 2, episode: 1 },
      { id: 'p5', title: 'P5', season: 2, episode: 2 },
    ]},
    { number: 3, episodes: [{ id: 'p6', title: 'P6', season: 3, episode: 1 }] },
  ],
};
app.state.detailsProgress = { items: [
  { episode_id: 'p1', position: 1200, duration: 1200, completed: true },
  { episode_id: 'p2', position: 400,  duration: 1200, completed: false },
  { episode_id: 'p4', position: 1200, duration: 1200, completed: true },
  { episode_id: 'p5', position: 1200, duration: 1200, completed: true },
]};
app.renderEpisodes(SEASONED);
const seasonPillState = p => p.classList.contains('watched') ? 'watched'
  : p.classList.contains('partial') ? 'partial' : 'none';
check('season with a finished, a mid-way and an untouched episode reads as partial',
  seasonPillState(qa('.season-pill')[0]), 'partial');
check('season finished end to end reads as watched',
  seasonPillState(qa('.season-pill')[1]), 'watched');
check('season with no progress at all carries neither class',
  seasonPillState(qa('.season-pill')[2]), 'none');
app.state.detailsProgress = null;

// The player's "next episode" button. The list it walks is the same flattened
// season order the details screen renders, so the interesting cases are the two
// ends of it and the seam between seasons.
console.log('--- next episode ---');
const at = (item, mediaId) => { app.state.current = item; app.state.episodeId = String(mediaId); };
const nextTitle = () => { const n = app.nextEpisode(); return n ? n.title : null; };

at(SERIES, SERIES.seasons[0].episodes[0].id);
check('next crosses a season boundary without season arithmetic', nextTitle(),
  SERIES.seasons[1].episodes[0].title);

at(SERIES, SERIES.seasons[1].episodes[0].id);
check('the last episode has no next', app.nextEpisode(), null);

const nextBtn = document.getElementById('nextEpisode');
at(SERIES, SERIES.seasons[0].episodes[0].id); app.updateNextButton();
check('the button is live while there is somewhere to go', nextBtn.disabled, false);

at(SERIES, SERIES.seasons[1].episodes[0].id); app.updateNextButton();
check('on the last episode it goes inactive but stays in place',
  [nextBtn.disabled, nextBtn.classList.contains('is-disabled'), !!nextBtn.parentNode],
  [true, true, true]);
check('and says why', nextBtn.getAttribute('title'), 'Это последняя серия');

at(MOVIE, (MOVIE.media && MOVIE.media[0] && MOVIE.media[0].id) || 'x'); app.updateNextButton();
check('a movie has no next episode either', nextBtn.disabled, true);

app.state.current = { id: 'tv-1', title: 'Канал', live: true };
app.updateNextButton();
check('nor does live TV', nextBtn.disabled, true);

// Autoplay. The setting shipped long before anything read it, so these pin
// down that the reader exists and that it stays quiet in every case where
// jumping to another episode would be wrong.
console.log('--- autoplay next ---');
at(SERIES, SERIES.seasons[0].episodes[0].id);

app.state.settings = { autoplay_next: true };
check('with the setting on, the next episode is picked up', app.autoplay(), true);

app.state.settings = { autoplay_next: false };
at(SERIES, SERIES.seasons[0].episodes[0].id);
check('with the setting off, nothing happens', app.autoplay(), false);

app.state.settings = { autoplay_next: true };
at(SERIES, SERIES.seasons[1].episodes[0].id);
check('the last episode ends the series instead of looping', app.autoplay(), false);

at(MOVIE, (MOVIE.media && MOVIE.media[0] && MOVIE.media[0].id) || 'x');
check('a movie never autoplays', app.autoplay(), false);

app.state.current = { id: 'tv-1', title: 'Канал', live: true };
check('live TV never autoplays', app.autoplay(), false);

app.state.settings = {};
at(SERIES, SERIES.seasons[0].episodes[0].id);
check('settings that never loaded are treated as off', app.autoplay(), false);

// The button (and autoplay) read state.episodeId, and only openUrl() ever sets
// it. play() used to call updateNextEpisodeButton() twice, both times BEFORE
// openUrl ran - so the button always reflected the *previous* episode, one
// step behind reality. On a session's very first play that previous id is ''
// (index -1, "not found"), which disabled the button even with a real next
// episode sitting right there; it looked keyed to "has the next episode been
// watched" only because normal sequential viewing made the off-by-one drift
// land on the true last episode at exactly that point. Driving the real
// play() promise chain (not just poking state.episodeId by hand, which is
// what every check above this one does) is the only way to catch that class
// of bug.
console.log('--- next-episode button through a real play() call ---');
const flushMicrotasks = () => new Promise(r => setTimeout(r, 0));

(async () => {
  app.state.settings = { autoplay_next: false, stream_mode: 'auto' };
  app.state.episodeId = ''; // a fresh session: nothing has played yet

  await app.play(SERIES, SERIES.seasons[0].episodes[0], 0);
  await flushMicrotasks(); await flushMicrotasks();
  check('first-ever play of a non-last episode leaves the button enabled',
    nextBtn.disabled, false);
  check('and it points at the real next episode, not the one just started',
    app.nextEpisode() && app.nextEpisode().id, 'b1');

  await app.play(SERIES, SERIES.seasons[1].episodes[0], 0);
  await flushMicrotasks(); await flushMicrotasks();
  check('playing the true last episode disables the button', nextBtn.disabled, true);

  console.log(failures ? `\n${failures} FAILURE(S)` : '\nAll checks passed');
  process.exit(failures ? 1 : 0);
})();
