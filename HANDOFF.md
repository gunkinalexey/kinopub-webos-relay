# Handoff — kinopub-webos-client

Written 2026-08-07 to continue this work in a fresh context window. Read this
first, then `README.md` (full changelog, newest first) if you need detail on
a specific past change.

## Two things left mid-conversation — check these before anything else

1. ~~**"Я смотрю" section is still wrong, blocked on the user.**~~ **Resolved.**
   User sent `kinoapi.com/api_watching.html` (docs page, not a DevTools
   capture) instead. It documents `v1/watching/serials?subscribed=1` (the
   real "Буду смотреть" filter — confirmed live, collapses the previous
   28-item unfiltered list down to exactly the one card the user had shown,
   "Stuart Fails to Save the Universe") and `v1/watching/togglewatchlist?id=`
   (a real `v1/` REST endpoint, `GET`, returns `{"watching": bool}` — not the
   session-cookie website route guessed earlier). Both verified live via
   `/bridge/explorer` before shipping, including a round-trip toggle to
   confirm it doesn't leave the real account's state changed. `/catalog/
   watching` now passes `subscribed=1`; new `POST /catalog/items/{id}/
   watchlist` wraps the toggle; a green-eye "Я смотрю" button on the serial
   details screen (top-right, symmetric to "← Назад") calls it. See
   `README.md` v0.9.80 (backend) for the full writeup.

2. **The "4K" poster badge is probably misleading — asked the user, no
   answer yet.** Added this session (`posterBadges()` in
   [app.js](frontend/app.js), driven by `item.quality` from
   `normalize_catalog_item` in [main.py](backend/app/main.py)). Just
   discovered: catalogue `quality` (used for the 4K-vs-HD badge decision)
   does **not** reliably match what's actually encoded and playable.
   Checked 23 titles the catalogue tags `quality: 2160` — **0 of 23** had an
   actual 2160p file in `v1/items/{id}`'s raw `videos[].files[]` (confirmed
   in the *raw* KinoPub response, before any of this backend's processing —
   not something we're filtering out). `quality` on the list endpoint looks
   like a "what the source was" tag, not "what's streamable". Asked the user
   whether to (a) drop the 4K badge since it's currently never true for
   anything checked, or (b) keep it but relabel so it doesn't promise
   playback that won't happen. Waiting on their call.

## What this project is

A lightweight KinoPub web client for LG webOS TV browsers (and desktop as a
secondary target). FastAPI backend (`backend/app/main.py`, ~2500 lines, one
file) bridges the KinoPub API — auth, catalogue, streaming, image proxy — to
a vanilla-JS frontend (`frontend/app.js`, one file, one closure, no build
step, no framework). `frontend/index.html` + `styles.css` round it out.
`docker-compose.yml` runs both: backend built from `backend/Dockerfile`
(includes ffmpeg), frontend is `nginx:1.27-alpine` bind-mounting `./frontend`
read-only — **frontend changes are live immediately, no rebuild**.

Backend version string lives at `app = FastAPI(..., version='0.9.NN', ...)`
in `main.py` near the top; bump it whenever `backend/` changes so `/health`
reflects what's actually deployed.

The real upstream API is `https://api.service-kp.com` (`API_BASE`), same
family documented (patchily, and `WebFetch` reliably `ECONNRESET`s on that
domain — use `WebSearch` instead, it works) at kinoapi.com. The built-in
`GET /explorer?path=&query=` endpoint (backed by `safe_explorer_path`) is the
fastest way to ground-truth a real endpoint against the live account instead
of guessing from docs — used constantly this session, keep using it.

## Current state

- Branch: `rework/audio-subtitles-details` (not merged to `main`)
- Backend running version: **0.9.79**, containers up via `docker compose up -d`
- `curl http://localhost:8080/bridge/health` to check it's alive
- Working tree clean — the auto-commit hook (see below) picks up every turn
- 159 frontend checks + 17 backend smoke checks, all green (see Testing below)

## How work has been happening (read this before doing anything)

**A `Stop` hook auto-commits and rebuilds after every assistant turn.**
Configured in `.claude/settings.json` → `.claude/hooks/checkpoint.sh`. After
each response: `git add -A && git commit -m "checkpoint: ..."`, then if
`backend/` changed, `docker compose up -d --build backend`. This means:

- You do not need to (and should not try to) manually commit — it happens
  automatically at turn end. Committing mid-turn yourself just adds noise.
- Commit messages are auto-generated (`checkpoint: <dirs>,... (<timestamp>)`)
  and carry no meaning beyond "these paths changed". Do not treat git log as
  a changelog — **`README.md` is the changelog**, written in Russian, newest
  entry first, one section per user-visible change. Keep adding to it the
  same way: a `## vX.Y.Z — <short title>` heading, then prose explaining
  *why*, not just what.
- `rebuid_containers.bat` (typo is original, not mine) is NOT what the hook
  runs — that script does `docker compose build --no-cache` (reinstalls
  ffmpeg every time, ~480MB, slow) and ends in `pause` (hangs forever
  headless). It's kept only for manual full rebuilds. The hook uses
  `docker compose up -d --build backend` (cached, fast, no pause).
- `examples/` (kino.watch saved pages used as ground-truth for what the real
  site does, and — discovered this session — the real site's own bundled JS
  with actual endpoint/route names) is gitignored — each saved page carries
  a live `csrf-token` from the user's actual session. Never `git add -f` it.
  Worth grepping before guessing an endpoint name: e.g.
  `examples/ex1/.../combined-9ff94152d4.js.загружено` has the real
  watchlist-subscribe click handler.

**Docker on this machine**: `docker run`/`docker compose` are unreliable
through the Bash tool (path-mangling on Windows — `-w /app` etc. sometimes
resolve to a Windows path and fail). Use the **PowerShell** tool for every
`docker`/`docker compose` call instead; it's been reliable all session.

**The user communicates in Russian** and expects responses in Russian.
They're technical, push back hard when something is hand-wavy or guessed,
and routinely catch real bugs or wrong assumptions from a single screenshot
or terse correction (e.g. "логика не такая, вот единственное, что я должен
был получить" — no elaboration, and it was right: the whole `/catalog/history`
scan approach for "Я смотрю" was conceptually wrong, not just buggy). Do not
present a guess as a finished answer — when an API shape is uncertain,
**verify it live via `/explorer` before shipping**, the way the two open
items above still need to be. When told something's wrong, re-derive from
scratch rather than patching the wrong theory.

## Everything that's been fixed/built this session, in order (README.md has full detail, this is the map)

Versions 0.9.75 → 0.9.88 (frontend), backend 0.9.74 → 0.9.79:

1. **401 mid-session → device-code gate, not a raw error string.** Any
   authenticated `KPApi` call failing with a real 401 now re-shows the same
   pairing screen as first launch (`handleSessionExpired()`), instead of
   leaving `{"detail": "..."}"` printed into the catalogue grid.
2. **Catalogue page size fills whole rows** (`catalogPerPage()` reads
   `getComputedStyle(...).gridTemplateColumns` to size to the real column
   count, ~50 items) instead of a flat 48 that left a ragged last row.
3. **Search suggestions dropdown**: 14 rows visible (was 10), taller
   `max-height`.
4. **History date bug** (biggest one): `_history_entry_item` picked `time`
   (seconds *watched*, not a timestamp) over `last_seen` because of
   dict-order priority in `_pick_first` — every entry showed "1 января
   1970". Also added per-episode `S01E02 · title` tag and a real per-episode
   frame thumbnail (`media.thumbnail`) instead of the show's poster, gated
   behind a new setting (`history_episode_frames`, default on).
5. **Episode carousel**, redesigned twice this session based on real
   feedback: started as scroll-with-overlay-arrows (arrows covered the edge
   card, stole its click), now a true paged carousel —
   `wireEpisodeCarousel()` in [app.js](frontend/app.js) clips to whole cards
   only via a JS-computed `max-width` (never a fraction of a card visible),
   arrows are flex siblings sized to match the card row (not
   `position:absolute` over it), dots below jump to a page. Episode cards
   also now show ПРОСМОТРЕНО/ПРОДОЛЖИТЬ overlays, and the carousel
   auto-opens on the first unwatched episode (last block if everything's
   watched) — merges two watched-signals, see #8.
6. **Site-wide scrollbar restyled** (thin, rounded, on-theme) instead of
   each browser's OS default.
7. **Poster badges made honest**: dropped the always-on "Субтитры" icon
   (catalogue list payload has no subtitle field at all, only the
   per-item detail fetch does — showing it was outright fabricated).
   Dolby/HD driven by real `ac3`/`quality` fields. **The HD/4K half of this
   is now suspect — see open item #2 above.**
8. **`v1/watching?id=`** (per-item watched status, real cross-device data)
   added as `GET /catalog/items/{id}/watching`, merged with local
   `/history` progress for episode watched-marks (`episodeWatched()`) — two
   real bugs found building this: (a) movie vs. serial shape differs
   (`videos[]` vs `seasons[].episodes[]`) and the first version only handled
   the movie shape; (b) `status` is `-1`/`0`/`1`, not a plain bool — `bool(-1)`
   is `True` in Python, so the first version marked everything watched.
9. **A real race condition**: `loadItemProgress`/`loadItemWatching`'s
   callbacks re-rendered the episode strip using the `item` argument
   captured at `openDetails()` call time — a summary card stub with no
   `.seasons` — instead of `state.current` (which `renderDetails(full)`
   upgrades to the enriched item once `KPApi.item()` resolves). Harmless
   while local `/history` usually won the race; adding the slower
   `/watching` call made losing it common — the strip would render
   correctly, then blank itself out when the slow call finally returned.
   Both functions now read `state.current`, not `item`.
10. **Vote buttons wired to a real endpoint** — `v1/items/vote?id=&like=`,
    exposed as `POST /catalog/items/{id}/vote`. Verified live against the
    real API (cast an actual vote, count moved).
11. **Details backdrop taller**, fade extends further down toward the
    episode strip instead of cutting off right under the Watch button.
12. **Movie resume button no longer shows "Серия 1"** — that string is a
    backend placeholder title for a nameless media file, meant for real
    multi-episode strips; `episodeLabel()` now suppresses it when the title
    is a single file.
13. **Sidebar wheel-scroll redirected** to the content pane — the sidebar's
    own `overflow:auto` was capturing scroll wheel input meant for the grid.
14. **"Я смотрю" section** — resolved in a later session, see item #1 at the
    top and README.md v0.9.80 (backend).
15. **Browser back/forward + reload keeps your place** — `location.hash`
    only (`#route/x`, `#details/id`, `#search/mode/query`); `route()` /
    `openDetails()` / `doSearch()` push their own hash, one `hashchange`
    listener (`applyHash()`) both restores on load and reacts to
    back/forward. `pushHash()`/`parseHash()` guard `typeof location===
    'undefined'` — required, since `frontend/tests/*.js` call `route()` etc.
    directly against a bare Node global with no real `location`.

## Known gaps / things flagged but not done

Carried forward from before, still true unless someone's addressed them:

- **Settings that don't do anything**: `audio_language` and `autoplay_next`
  are saved and never read. `reduce_motion` toggles a CSS class that doesn't
  exist in `styles.css`.
- **Search falls back to a mock catalogue on *any* error**, not just
  KinoPub-unreachable — `KPApi.search` in `api.js`.
- **Progress/"continue watching" for the *button*, not the episode-card
  marks, is still local-only.** (Episode-card watched marks now use real
  `v1/watching` data too, per #8/#9 above — but the Continue button's own
  resume position still only reads this bridge's local SQLite, never
  KinoPub's own cross-device position.)
- **FFmpeg removability still an open question** — never got a definitive
  answer on whether the remux fallback is ever actually reached in real use.
- **`WITH_FFMPEG` build-arg config** — asked for once, never implemented.
- **Backdrop `big` poster size barely matters** at current CSS widths unless
  DPR>1 — noted as a caveat, not fixed.

New from this session:

- **4K badge accuracy — see open item #2 at the top.**
- **`v1/collections`** (real curated collections/подборки) and
  **`v1/bookmarks`** (real per-account bookmark folders — the "Закладки"
  sidebar button is still dead, no `data-route`) are both confirmed-real,
  unused endpoints, found during the API survey that led to items #1/#8.
  Not requested yet, but the user may come back to either.
- **`v1/tv`** is a real live-TV-channel list (m3u8 streams, includes actual
  sport channels — ESPN, Eurosport, Fox Sports, TNT Sport UHD). The "Спорт"
  sidebar section is still just a VOD catalogue filter, not this. User was
  told this exists; hasn't asked for it to be built yet.

## Testing infrastructure

**Frontend**: `frontend/tests/`, eight plain Node scripts, no framework —
`frontend/app.js` is a single IIFE with no module boundary, so each test
file `eval`s the real source with the closing `}());` swapped to also expose
functions under test on `global.__app`. Four use hand-rolled DOM stubs
(fast, no deps); four (`sections.js`, `actions.js`, `episodes.js`,
`panel.js`) load the real `index.html` into `jsdom`. See
`frontend/tests/README.md` for the run recipe. **159 checks, 0 failures**.
Run via (PowerShell, not Bash — see Docker note above):
```
docker run --rm -v "D:\pets\kinopub-webos-client\frontend:/f:ro" -w /tmp node:20-alpine sh -c "cp -r /f /tmp/frontend && cd /tmp/frontend/tests && npm install --silent >/dev/null 2>&1 && node harness.js ../app.js && node subs.js ../app.js && node misc.js ../app.js && node quality.js ../app.js && node sections.js ../app.js && node actions.js ../app.js && node episodes.js ../app.js && node panel.js ../app.js"
```
Also run `node --check app.js` and, ideally,
`npx eslint --no-eslintrc --env browser,es2020 --parser-options=ecmaVersion:2020 --rule '{"no-undef":"error"}' app.js`
(install eslint@8 into the same throwaway container) after any change —
caught real bugs this session (e.g. a wrapper IIFE accidentally matching the
test harness's `}());`-replace trick, silently truncating instrumented code).

**Backend**: `backend/smoke_test.py`. Boots the real FastAPI app with a
seeded SQLite session, hits every endpoint that doesn't need live KinoPub.
**17 checks, 0 failures**. Run via:
```
docker run --rm -v "D:\pets\kinopub-webos-client\backend:/src:ro" -w /app kinopub-webos-client-backend sh -c "cp /src/smoke_test.py . && rm -rf /app/app && cp -r /src/app /app/app && python smoke_test.py"
```
Also run `python -m py_compile` on a copy of `app/` (the mounted `:ro` volume
can't write `__pycache__` in place) before that, since no `pyflakes` install
is reachable in this sandbox (no network egress to PyPI) — `py_compile` at
least catches syntax errors.

**Both are ad-hoc verification scripts that accumulated over many turns, not
a designed test suite.** Extend in the same spirit rather than replace with
a framework, unless asked.

## If you're picking this up cold, do this first

1. **Read the two open items at the very top of this file first** — one is
   fully blocked on the user (don't guess a third "Я смотрю" endpoint), one
   just needs their answer relayed into a small code change.
2. `cd D:/pets/kinopub-webos-client && git log --oneline -5` and
   `curl http://localhost:8080/bridge/health` — confirm what's actually
   running vs. what's in git.
3. Skim `README.md` top-to-bottom (newest-first) for fuller detail behind
   any bullet above.
4. If touching `frontend/app.js`: run the suite in `frontend/tests/` before
   and after (see Testing above) — most regressions this session were
   single-string-replace mistakes or a stale variable captured across an
   async boundary (see item #9 above), not logic errors the tests can't see.
5. If touching `backend/app/main.py`: `py_compile`, then
   `backend/smoke_test.py`, then bump the version string and rebuild via
   PowerShell (`docker compose up -d --build backend`) — verify
   `/bridge/health` actually reports the new version before calling it done.
6. When an API shape is uncertain, use `GET /bridge/explorer?path=&query=`
   against the live account before writing code against a guess — it's
   faster and more reliable than the docs site, and this session's biggest
   mistakes (both open items) came from acting on a plausible-looking guess
   instead of checking first.
