# Frontend regression suite

Plain Node scripts, no test runner or framework. Each `eval`s the real
`frontend/app.js` inside a stubbed (or, for the four jsdom-based ones, real)
DOM, drives it, and prints `PASS`/`FAIL` lines. No network, no KinoPub
credentials — `KPApi` is stubbed per file.

These accumulated turn by turn while fixing specific bugs; they were never
designed together as a suite. Each file is independent and can be read on its
own to see what behaviour it pins down.

## Why plain scripts, not Jest/Vitest

`frontend/app.js` is a single self-invoking IIFE with no module boundary and
no build step (it ships straight to the browser via a `<script>` tag). Rather
than restructure it for a test framework, each script `eval`s the source with
its closing `}());` swapped for one that also assigns the functions/state
under test to `global.__app`, so they become reachable from the test file
without changing the shipped code at all.

## Two kinds of file

**Stub-DOM** (`harness.js`, `subs.js`, `misc.js`, `quality.js`): hand-rolled
fake `document`/`video` objects, just enough surface for the code path under
test. Fast, no dependencies.

**Real-DOM** (`sections.js`, `actions.js`, `episodes.js`, `panel.js`): load
the actual `frontend/index.html` into `jsdom` and drive real elements —used
when the test needs to check actual rendered structure/CSS classes rather
than call counts. Needs `npm install` once (`jsdom` — see `package.json`).
`panel.js` additionally writes `preview.html` next to itself so you can open
it in a browser and eyeball the layout; it is git-ignored, regenerated on
every run.

## Running

No local Node needed — none was installed on the dev machine this suite was
built on, so every run in this repo's history went through a `node:20-alpine`
container. From `frontend/tests/`:

```bash
# once, for the jsdom-based files
npm install

# each file takes the path to app.js as its one argument
node harness.js  ../app.js
node subs.js     ../app.js
node misc.js     ../app.js
node quality.js  ../app.js
node sections.js ../app.js
node actions.js  ../app.js
node episodes.js ../app.js
node panel.js    ../app.js   # also writes preview.html
```

Or all at once, via Docker, from the repo root — this is the exact command
used to verify this suite after every change to `frontend/app.js`:

```bash
docker run --rm -v "$PWD/frontend:/f:ro" -w /tmp node:20-alpine sh -c '
  cp -r /f ./frontend && cd frontend/tests
  npm install jsdom --silent --no-fund --no-audit
  for s in harness.js subs.js misc.js quality.js sections.js actions.js episodes.js panel.js; do
    echo "=== $s ==="; node "$s" ../app.js
  done'
```

As of the last run: **276 checks, 0 failures** across all eight files.

## What each file covers

| File | Covers |
|---|---|
| `harness.js` | Audio-track switching ladder (hls.js alt-audio → native `audioTracks` → alternate HLS variant → FFmpeg remux), ordinal-vs-`index` track addressing |
| `subs.js` | Subtitle mount/rebuild loop fix, per-track `shift`, language auto-selection, embedded-track menu listing |
| `misc.js` | Remote OK-key-on-timeline fix (no false seek-to-0), shared label-building helpers (`pushLabelPart`/`truthyFlag`), `play()` promise rejection handling (AbortError silencing, NotAllowedError messaging) |
| `quality.js` | Device-capability probing (HEVC/HDR/MSE) including the three-way "browser did not answer" case and the multi-spelling codec probe, what gets declared to KinoPub, the explicit device profile, quality-variant selection and ranking, stream-mode fallback on Direct-playback failure, fullscreen mode selection |
| `sections.js` | 3D as its own catalogue section, Фильмы/3D heading toggle, History section (day grouping, type filter, per-filter paging), "Я смотрю" sidebar badge on startup, its Новые эпизоды/Мои сериалы toggle (two different endpoints, not two slices of one), real catalogue filters (genre/country/year/quality/sort, per-section state, reset), "Закладки" (folder list, folder contents, back navigation, resets when you leave and come back), the kino.pub-style filter panel: two-handle range sliders driven from a remote (OK enters edit mode, OK swaps handle, arrows move, Back exits), debounced commit, focus surviving the panel rebuild |
| `actions.js` | Details-screen Watch/Continue/Restart buttons, resumable-position heuristics, that a fresh title doesn't inherit the previous title's resume position |
| `episodes.js` | Season-picker/episode-strip visibility rules: hidden for a single-file movie, flat strip (no season pills) for one season with multiple episodes, season pills only for genuinely multiple seasons. Also the "Серии:" episode-pill row — its own class rather than `.season-pill`, rebuilt per season, watched filled vs part-watched outlined |
| `panel.js` | Full details-screen render: title/rating/vote/tab/info-table structure, season-switching replaces (not appends) the episode strip, screen-not-overlay navigation (Back returns to wherever it was opened from), the "Похожие" block (hidden on an empty answer, one card per title, late answers discarded) |

## Fixtures

`fixture.json` — a synthetic "Тед Лассо" (Ted Lasso) item: 4 seasons, rich
audio/subtitle metadata, votes, cast. Used by `panel.js`, `actions.js`
(imported inline, not from this file — see the top of `actions.js`).

## Backend

The backend equivalent is `backend/smoke_test.py` — not here, see that file's
docstring. Different mechanism (`fastapi.testclient.TestClient` against the
real app with a seeded SQLite session), same purpose: catch response-model
mismatches and route wiring bugs that `pyflakes`/`ast.parse` cannot see.
