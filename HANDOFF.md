# Handoff — kinopub-webos-client

Written 2026-08-06 to continue this work in a fresh context window. Read this
first, then `README.md` (full changelog, newest first) if you need detail on
a specific past change.

## What this project is

A lightweight KinoPub web client for LG webOS TV browsers (and desktop as a
secondary target). FastAPI backend (`backend/app/main.py`, ~2300 lines, one
file) bridges the KinoPub API — auth, catalogue, streaming, image proxy — to
a vanilla-JS frontend (`frontend/app.js`, one file, one closure, no build
step, no framework). `frontend/index.html` + `styles.css` round it out.
`docker-compose.yml` runs both: backend built from `backend/Dockerfile`
(includes ffmpeg), frontend is `nginx:1.27-alpine` bind-mounting `./frontend`
read-only — **frontend changes are live immediately, no rebuild**.

Backend version string lives at `app = FastAPI(..., version='0.9.NN', ...)`
in `main.py` near the top; bump it whenever `backend/` changes so `/health`
reflects what's actually deployed.

## Current state

- Branch: `rework/audio-subtitles-details` (not merged to `main`)
- Backend running version: **0.9.74**, containers up via `docker compose up -d`
- `docker compose exec backend` / `curl http://localhost:8080/bridge/health`
  to check it's alive
- Working tree: only `.gitignore` and the new `frontend/tests/` directory are
  uncommitted as of this handoff — the auto-commit hook (see below) will
  pick them up after this turn ends
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
  site does) is gitignored — each saved page carries a live `csrf-token`
  from the user's actual session. Never `git add -f` it.

**The user communicates in Russian** and expects responses in Russian.
They're technical, they push back if something is hand-wavy, and they've
caught real bugs I introduced by their symptom reports alone (e.g. "no
refresh token" → traced to a `Content-Length` mismatch, not the OAuth issue I
initially suspected). Take symptom reports at face value, verify against the
running server, don't assume the first plausible theory is correct.

## Everything that's been fixed/built, in order (see README.md for full detail)

Versions 0.9.60 → 0.9.75 (current: 0.9.74, since the last change was
frontend-only and didn't bump the backend version — this is a minor
inconsistency, not a bug):

1. **Audio track switching** — was always going straight to FFmpeg remux.
   Now a ladder: hls.js alternate-audio renditions → native
   `video.audioTracks` → reload an alternate KinoPub HLS variant → FFmpeg
   remux only as last resort. Fixed `audios[].index` (KinoPub's per-file
   number, 0- or 1-based depending on payload) being fed directly into
   `-map 0:<n>` — now addressed by *ordinal position*, matching `-map 0:a:N`.
2. **Subtitles** — infinite `<track>`-rebuild loop (appending a `<track>`
   fires `addtrack`, whose handler was calling back into the same rebuild
   function). Added `::cue` CSS (there was none — subtitles rendered at
   browser default size), wired up the `subtitle_size` setting (was
   hardcoded to 100), per-track `shift`, language auto-selection at
   playback start.
3. **Codebase audit** — removed dead routes/functions, fixed a real bug
   (remote OK key on the video timeline seeking to 00:00 because
   `activeElement.click()` produces a synthetic event with `clientX=0`).
4. **`play()` promise rejections** — `AbortError` from `pause()`/`load()`
   during normal stream switching was being reported as a playback error
   ("The play() request was interrupted..."). Now silenced unless it's a
   genuine failure; `NotAllowedError` gets an actionable "press OK" message.
5. **Item details panel** — rebuilt to match the real kino.watch item page
   (poster, vote counts, tabs, characteristics table, season/episode strip),
   turned from a modal overlay into a fourth screen (alongside
   catalogue/search/settings) so it doesn't cover the grid.
6. **Picture quality** — `choose_best_stream` was ranking by `abs(height -
   1080)`, which penalized 2160p as badly as 0p, so 4K was never picked and
   HEVC was actively deprioritized (which is where KinoPub's 10-bit/HDR
   variants live). Rewritten to rank by resolution first, HEVC-over-H.264 at
   equal size second. Frontend probes device capabilities
   (`canPlayType`/`MediaSource.isTypeSupported` for HEVC, `dynamic-range`,
   `color-gamut`) and picks the best variant the device can actually decode,
   preferring Direct playback over hls.js/MSE for hardware-decoded HEVC
   (MSE commonly drops HDR to SDR on webOS).
7. **Fullscreen mode** — configurable (`layer` keeps custom controls, `video`
   fullscreens the media element itself for the best shot at HDR
   passthrough, `off`). Must be requested synchronously inside the click
   handler (user-activation window closes by the time an async API call
   resolves).
8. **History/3D/Anime sections** — "Аниме" isn't a KinoPub content *type*,
   it's genre id 25 (`/v1/items?type=anime` returns nothing); the site's
   `/anime` page is a genre filter. 3D turned out to be its own real type
   (`kino.watch/3d`), previously the "3D" link just re-routed to the plain
   movie list. Added a working History section
   (`GET /catalog/history`) grouped by day with a type filter — discovered
   `v1/history`'s `type` param is silently ignored upstream (always returns
   the same page), so the backend scans up to 20 upstream pages and filters
   locally when a type is selected, with a 3-minute cache.
9. **Two regressions from my own edits, both caught by the user via
   symptom, not by me proactively**:
   - `catalog_list` referenced a variable (`api_type`) that no longer
     existed after a refactor → `NameError` → 500 on every catalogue
     section. `ast.parse`/syntax checks don't catch this.
   - `/auth/status`'s return-type annotation (`Dict[str, bool]`) wasn't
     updated when `expires_in` (an int) was added to the response — FastAPI
     validates responses against the annotation, so every authenticated call
     500'd, which looked exactly like "login doesn't survive a reload."
   - **Consequence**: added `pyflakes` (backend) and `eslint --rule no-undef`
     (frontend) to the verification routine, and `backend/smoke_test.py` — a
     script that boots the app with a real seeded session and hits endpoints
     that don't need live KinoPub, specifically to catch response-model
     mismatches like the second bug. Run it after *any* backend change:
     ```bash
     docker run --rm -v "$PWD/backend:/src:ro" -w /app kinopub-webos-client-backend \
       sh -c "cp /src/smoke_test.py . && python smoke_test.py"
     ```
   - Separately (not a regression, a real bug found by symptom): the device
     pairing screen never dismissed after successful pairing, and each retry
     created a new orphaned session server-side. Cause:
     `JSONResponse(..., headers=dict(response.headers))` copied a
     `content-length: 0` from an injected empty `Response` object onto a
     reply that actually carried 23 bytes of JSON — the client read zero
     bytes, JSON parsing failed, the "authorized" branch never ran, but the
     `Set-Cookie` had already gone out so the session *was* created
     server-side. Fixed by setting the cookie directly on the response
     that's actually returned.
10. **Real 16:9 backdrops** — KinoPub's CDN serves `poster/item/wide/{id}.jpg`
    (up to 3840×2160) alongside the usual 2:3 `medium`/`big` posters, unused
    until now. Backdrop was being built from a stretched 250×375 poster.
    Image proxy gained a `fallback` param (CSS `background` can't retry a
    404) since ~a handful of items lack `wide`.
11. **Details-screen layout reorder** — actions (Watch/Continue/Restart) and
    the season/episode picker moved above the fold, under the title, instead
    of being buried in the poster column / below the whole info table.
    Continue/Restart buttons appear only when there's a genuinely mid-way
    saved position (not first 30s, not last 60s, not `completed`); Continue
    resumes the *specific episode* that has the saved position, not episode
    1. Fixed a real bug found while building this: `play()` never reset
       `playerResumePosition`, so opening a second title after watching part
       of a first one resumed at the *first title's* timestamp. Start
       position is now an explicit third argument: `play(item, episode,
       startAt)`.
12. **Season/episode selector visibility (most recent, uncommitted-by-hook
    at time of writing)** — backend always synthesizes at least a pseudo
    "season 1" (`entry.get('season') or 1`), so `item.seasons` was never
    empty and a single-file movie got a one-item season picker + one-episode
    strip it had no business having. Frontend rule is now: 2+ real seasons →
    season pills; 1 season (real or synthesized) with 2+ episodes → flat
    episode strip, no pills; a single file → nothing, the Watch button is
    enough. The `S01E01`-style code on episode cards / resume labels is now
    also gated on *genuinely* multiple seasons.

## Known gaps / things flagged but not done

From various turns' "what I didn't do" notes — still true unless someone's
addressed them since:

- **Settings that don't do anything**: `quality`'s old three-way select was
  replaced with a real ceiling (auto/2160/1080/720, works), but
  `audio_language` and `autoplay_next` are still saved and never read.
  `reduce_motion` toggles a CSS class (`reduce-motion`) that doesn't exist in
  `styles.css`.
- **Search falls back to a mock catalogue on *any* error** (expired session,
  network blip, real 500), not just when KinoPub is genuinely unreachable —
  `KPApi.search` in `api.js`. An expired session currently makes search
  silently show "Дюна"/"Оппенгеймер" instead of an auth error.
- **Progress/"continue watching" is local-only** — `/history` (this bridge's
  own SQLite `watch_progress` table, distinct from `/catalog/history` which
  proxies KinoPub's real viewing history). "Continue" on the details screen
  only works for things watched *through this client*; it doesn't read
  KinoPub's own resume position. Could be worth reconciling if the user asks.
- **Episode cards don't show a progress bar** on the thumbnail even though
  the position data is already being fetched (`loadItemProgress`) — the site
  has this, we don't yet.
- **FFmpeg removability is still an open question** — added device/stream
  diagnostics (`GET /media/audio-variants`, Diagnostics screen shows decoded
  resolution, HDR/HEVC capability, fullscreen state) specifically so the
  user can determine from real usage whether the FFmpeg remux fallback is
  ever actually reached. Never got a definitive answer; the Dockerfile still
  installs it (~480MB via `apt-get install ffmpeg`, no `--no-cache-dir`
  savings possible there).
- **`WITH_FFMPEG` build-arg config** — user asked for it once, was never
  actually implemented (got interrupted by other more urgent bugs). If asked
  again: add an `ARG`+conditional `RUN` in `backend/Dockerfile`, a
  build-arg passthrough in `docker-compose.yml`, and make
  `_ffmpeg_http_reconnect_options()`/the audio-hls endpoints degrade
  gracefully (they already do — `FileNotFoundError` on `ffmpeg`/`ffprobe`
  raises a clean `HTTPException(500, 'FFmpeg is not installed')` — the
  frontend already surfaces that via `failAudioHls`, this was verified
  working, just never wired into the Docker build itself).
- **Backdrop `big` poster size (500×750) barely matters visually** at
  current CSS poster-slot widths (250px) unless the viewer has DPR>1 (i.e.
  the TV, likely) — noted to the user as an honest caveat, not fixed
  further.

## Testing infrastructure

**Frontend**: `frontend/tests/` (just checked into the repo this turn — it
previously only existed in a session-scoped scratchpad and would have been
lost). Eight plain Node scripts, no framework — `frontend/app.js` is a
single IIFE with no module boundary, so each test file `eval`s the real
source with the closing `}());` swapped to also expose the functions under
test on `global.__app`. Four use hand-rolled DOM stubs (fast, no deps); four
(`sections.js`, `actions.js`, `episodes.js`, `panel.js`) load the real
`index.html` into `jsdom` for structural assertions. See
`frontend/tests/README.md` for the exact run recipe — no local Node is
installed on this machine, everything ran through `node:20-alpine` in
Docker. **159 checks, 0 failures** as of this handoff; run them after any
`frontend/app.js` change.

**Backend**: `backend/smoke_test.py` (already committed pre-handoff). Boots
the real FastAPI app with `fastapi.testclient.TestClient` against a
temp SQLite DB with one seeded "authenticated" session row, hits every
endpoint that doesn't require a live KinoPub connection. Specifically
designed to catch response-model/annotation mismatches (see bug #9 above) —
`raise_server_exceptions=False` so a 500 shows up as a normal FAIL instead of
aborting the whole run. **17 checks, 0 failures**. Run via:
```bash
docker run --rm -v "$PWD/backend:/src:ro" -w /app kinopub-webos-client-backend \
  sh -c "cp /src/smoke_test.py . && python smoke_test.py"
```

**Both are ad-hoc verification scripts that accumulated over many turns, not
a designed test suite.** They're worth extending in the same spirit (small,
targeted, reads the real source) rather than replaced with a framework,
unless the user asks for that explicitly.

## If you're picking this up cold, do this first

1. `cd D:/pets/kinopub-webos-client && git log --oneline -5` and
   `curl http://localhost:8080/bridge/health` — confirm what's actually
   running vs. what's in git (the checkpoint hook should keep these in
   sync, but verify).
2. Skim `README.md` top-to-bottom (newest-first) for the fuller story behind
   any of the summary bullets above.
3. If touching `frontend/app.js`: run the suite in `frontend/tests/` before
   and after your change (see that dir's README for the exact command) —
   most past regressions were single-string-replace mistakes (wrong match
   count, shadowed variable, stale reference) that the suite catches
   immediately.
4. If touching `backend/app/main.py`: run `pyflakes`, then
   `backend/smoke_test.py`, then bump the version string and let the hook
   rebuild — or `docker compose up -d --build backend` yourself if you want
   to see the rebuild output.
