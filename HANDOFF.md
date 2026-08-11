# Handoff — kinopub-webos-client

Written 2026-08-07 to continue this work in a fresh context window. Read this
first, then `CHANGELOG.md` (full changelog, newest first) if you need detail on
a specific past change.

## Open items — check these before anything else

None blocking. Nothing is mid-conversation or waiting on the user right now.
Two things worth knowing about on pickup:

1. **`.env`'s `WITH_FFMPEG` is currently `0`** (backend running without
   FFmpeg - `/health` shows `"ffmpeg": false`). This was set to `0` by the
   user directly, between turns, apparently to try the no-FFmpeg build added
   this session (item #21 below); never confirmed whether that was meant to
   stick or was just testing. The only user-visible effect is the last rung
   of the audio-track ladder (`/audio-hls/jobs`, the server-side remux for a
   stream that genuinely carries just one audio track) being unavailable -
   the player already handles this gracefully (asks first, shows a real
   reason instead of a failed request), so nothing is broken, but it is
   worth asking the user whether they want `WITH_FFMPEG=1` back before
   assuming the current state is intentional. Changing it needs
   `docker compose up -d --build backend`, not a restart.

2. **HDR/direct root cause found and fixed; real-TV retest still pending.**
   The previous handoff blamed `watchDirectStall()` for this. That was
   wrong — the watchdog change shipped and the user came back with the same
   report ("не запускается direct и не цепляется hdr"). The actual cause was
   the device-capability reporting that landed in the same stretch (item #9
   below): probes that could not answer were written to KinoPub as explicit
   negatives. `supportHevc=0` makes KinoPub serve an h264-only ladder for
   every title — **verified live by toggling the flag and re-reading
   `v1/items/100468`** — so no HEVC file, no HDR file, and nothing for
   `preferredModeFor` to play direct with. Compounded by one KinoPub device
   record being shared by every browser on the bridge: a desktop visit
   stripping the TV's `supportHdr` was **reproduced live** during the fix.
   Fixed in backend 0.9.89 — multi-spelling codec probes, a real "unknown"
   state that never becomes "no", `Optional[bool]` on the backend so unknown
   flags are left alone, direct no longer vetoed by a single `canPlayType`
   string, plus a "Возможности устройства" setting (auto / TV / H.264-only)
   and diagnostics that show KinoPub's actual device flags via the new
   `GET /device/state`. See CHANGELOG's "Direct и HDR: приложение само отбирало
   их у телевизора" for the full trace and the flag matrix.
   **The device profile is currently set to "Телевизор"** on the user's
   account, which pins HEVC+4K+HDR regardless of what the TV's browser
   says. Still needs the user to confirm HDR is actually back on the TV; if
   it is not, the Diagnostics screen now answers the next question directly
   (`KinoPub: HEVC` and `Текущий вариант`/`Полный экран` rows), so ask for
   that screen rather than guessing. The file itself was already confirmed
   genuine HDR10 via `ffprobe` against the CDN URL (`color_transfer:
   smpte2084`, `color_primaries: bt2020`, both mastering-display and
   content-light-level side data) — never a content problem.
   Note: **direct playback cannot be reproduced in this sandbox** — the
   browser pane's network path to `*.cdntogo.net` hangs indefinitely
   (`readyState 0`, no bytes, fetch times out), while the exact same URL
   returns `206 Partial Content` from the host and from the backend
   container. Do not read a sandbox direct-playback failure as a bug.

(Everything else that came up this stretch - "Награды"/"Новые эпизоды" removed,
"Подборки" wired for real, the details-screen link badges, the search `field`
bug fix, etc. - is resolved, not open. See the numbered map below for what and
why; no follow-up needed on any of it.)

## What this project is

A lightweight KinoPub web client for LG webOS TV browsers (and desktop as a
secondary target). FastAPI backend (`backend/app/main.py`, ~2900 lines, one
file) bridges the KinoPub API — auth, catalogue, streaming, image proxy — to
a vanilla-JS frontend (`frontend/app.js`, one file, one closure, no build
step, no framework). `frontend/index.html` + `styles.css` round it out.
`docker-compose.yml` runs both: backend built from `backend/Dockerfile`
(includes ffmpeg), frontend is `nginx:1.27-alpine` bind-mounting `./frontend`
read-only — **frontend changes are live immediately, no rebuild**.

Backend version string lives at `app = FastAPI(..., version='0.9.NN', ...)`
in `main.py` near the top; bump it whenever `backend/` changes so `/health`
reflects what's actually deployed. **Currently: backend 0.9.97.**

The real upstream API is `https://api.service-kp.com` (`API_BASE`). Docs at
kinoapi.com are patchy and the domain is **intermittently unreachable via
WebFetch** (`ECONNRESET`, seemingly random — sometimes 10 retries in a row
fail, then it works fine for a while). Keep retrying rather than assuming a
specific page doesn't exist; if it stays down, `WebSearch` sometimes surfaces
a usable summary. The built-in `GET /bridge/explorer?path=&query=` endpoint
(backed by `safe_explorer_path`, GET-only, real-account, redacts tokens) is
the fastest and most reliable way to ground-truth a real endpoint — used
constantly, keep using it. It cannot do POST, so mutating endpoints
(device settings, bookmarks add/remove, etc.) need a throwaway script run
inside the backend container instead (see "Verifying live" below).

## Current state

- Branch: `rework/audio-subtitles-details` (not merged to `main`)
- Backend running version: **0.9.97**, containers up via `docker compose up -d`
- `curl http://localhost:8080/bridge/health` to check it's alive
- Working tree has uncommitted changes at handoff time — the auto-commit
  hook (see below) picks them up at the end of the current turn if this
  handoff is being read mid-session; if you're starting fresh, they're
  probably already committed.
- **312 frontend checks + 36 backend smoke checks, all green** (see Testing
  below)

## How work has been happening (read this before doing anything)

**A `Stop` hook auto-commits and rebuilds after every assistant turn.**
Configured in `.claude/settings.json` → `.claude/hooks/checkpoint.sh`. After
each response: `git add -A && git commit -m "checkpoint: ..."`, then if
`backend/` changed, `docker compose up -d --build backend`. This means:

- You do not need to (and should not try to) manually commit — it happens
  automatically at turn end. Committing mid-turn yourself just adds noise.
- Commit messages are auto-generated (`checkpoint: <dirs>,... (<timestamp>)`)
  and carry no meaning beyond "these paths changed". Do not treat git log as
  a changelog — **`CHANGELOG.md` is the changelog** (`README.md` is the project/deployment doc), written in Russian, newest
  entry first, one section per user-visible change. Keep adding to it the
  same way: a `## vX.Y.Z — <short title>` heading (or `## backend 0.9.NN —
  ...` when only the backend version moved), then prose explaining *why*,
  not just what.
- `rebuid_containers.bat` (typo is original) is NOT what the hook runs —
  that script does `docker compose build --no-cache` (reinstalls ffmpeg
  every time, slow) and ends in `pause` (hangs forever headless). Kept only
  for manual full rebuilds. The hook uses `docker compose up -d --build
  backend` (cached, fast, no pause) — use that yourself too.
- `examples/` (kino.watch saved pages, used as ground-truth for real page
  layouts) is gitignored — each saved page carries a live `csrf-token`.
  Never `git add -f` it. Contents right now: `ex1/` = "Спортивные
  трансляции" (sport TV page), `ex2/` = empty, `ex3/` = "Новые Эпизоды".

**Docker on this machine**: `docker run`/`docker compose` are unreliable
through the Bash tool (path-mangling on Windows). Use the **PowerShell**
tool for every `docker`/`docker compose` call instead — reliable all
session. Bash is fine for `curl`, `grep`, `git`, and reading files.

**`git commit` messages, `git log`, etc.**: also use Bash normally for these
(only `docker`/`docker compose` need PowerShell).

**The user communicates in Russian** and expects responses in Russian.
They're technical, push back hard when something is hand-wavy or guessed,
and routinely catch real bugs from a single terse observation ("в разделе
аниме намешиваются другие фильмы" — no elaboration, and it was exactly
right: the Anime section's own genre selector was being silently overwritten
by the filter panel's genre picker). Recurring pattern this session: user
reports a symptom in one sentence, the right move is to **reproduce it live
first** (via `/bridge/explorer` or a live browser check), find the actual
mechanism, *then* explain and fix — not guess-and-patch. Every fix in this
session was verified against the real account/API before being called done,
and every "this can't be done honestly" conclusion (finished status filter,
"Новые эпизоды") was reached by testing live and finding it genuinely
doesn't work, not by assumption. One of those conclusions - the
rating-range filter - was later found to be **wrong**: `conditions[]`
does accept `imdb_rating`/`kinopoisk_rating`, it just was not tried under
those names. Worth remembering as the failure mode of this method: "I
tested and it doesn't work" is only as good as the field names tried.

**Real endpoints are never guessed into existence.** When kino.watch's own
site shows some feature, the instinct is to guess a matching `v1/...` path —
this has burned time repeatedly (e.g. `/watchlist/subscribe/{id}` looked
right and wasn't; the real endpoint was `v1/watching/togglewatchlist`, found
only via docs). Always check kinoapi.com docs and/or `/bridge/explorer`
before writing code against a guessed shape.

**Never ship a filter/control that looks like it works but doesn't.** This
project has a running theme: several "obviously should exist" controls
(subtitle filter/badge, "Статус" serial-finished filter,
age/language/translation/voice-studio filters) turned out to have
**no real backing API field at all**, confirmed live - rating ranges were
on that list too until the right field names turned up, see item #17. The consistent policy
has been: don't fake it, don't add the UI control, say so in a comment/
changelog entry. Keep doing this — a user on this project would rather have
an honest smaller filter panel than a bigger one with dead switches.

## Everything fixed/built, most recent first (CHANGELOG.md has full prose, this is the map)

Backend 0.9.79 → 0.9.97 this stretch, frontend tests 159 → 312:

26. **Details-screen genre/country/year/director/cast badges are real links**
    now, matching kino.watch's own `movie?years=X;X` and `item/search?
    query=<name>&mode=director|actor`. Genre/country link only when KinoPub's
    payload actually carries a numeric id (`genres_detailed`/
    `countries_detailed`, `[{id,title}]` - an id-less entry stays plain text,
    never a link to nothing); year filters that exact year on the same
    section; director/cast open a real search in that person's own mode.
    **Found a real, pre-existing bug while verifying live**: the "Актёры"/
    "Режиссёры" search-mode tabs had been decorative from the start -
    `mode` was never turned into anything upstream, so both silently ran the
    same all-fields query as "Все". `v1/items/search`'s real `field` param
    (documented on `api_video.html`'s own search section, not a separate
    page this time - just easy to miss) was never wired. Fixed via
    `SEARCH_MODE_FIELDS`; verified live (`field=director` for a real name:
    13 titles, `field=cast`: 37, both zero before the fix).
25. **"Награды" and "Новые эпизоды" sidebar buttons removed** - see open item
    #2 above for the live-verified reason both are impossible via the API,
    not guessed. User's own call after being asked (defer / remove / other
    idea) - chose remove.
24. **История: "Все фильмы"/"Все эпизоды"** aggregate tabs, added alongside
    the existing per-type ones (kino.watch's own history page has both, not
    one instead of the other). `HISTORY_GROUPS` on the backend groups the
    same real `type` field client-side-of-upstream (`v1/history` has no
    `type` param at all) into the same standalone-vs-episodic split item #22
    already established for the duration display. Verified live: 148/852
    real entries on this account, cached like every other type filter.
23. **"Подборки" wired for real** (`v1/collections`, `v1/collections/view` -
    documented on `kinoapi.com/api_collections.html`, a page an earlier
    session's search missed). Three real sort tabs (Новые/Популярные/
    Просматриваемые - verified live, each a genuinely different first page);
    "Категории"/"Подписки" from kino.watch's own page are not offered (no
    matching real sort/endpoint). No pagination on the collection-view screen
    on purpose - upstream has none (a 67-item collection came back as one
    response, no `pagination` key). **Found and fixed a bug shared with
    "Закладки" while verifying live**: opening an item from inside a
    folder/collection, then pressing Back (details' own button, or the
    remote's hardware Back key - both go through `history.back()`), dropped
    straight to the top-level list instead of the folder/collection, because
    entering one never pushed its own hash entry. Fixed by extending
    `route(name, subId)` - the same call `applyHash()` now makes on the way
    back - so the hash trail matches the actual navigation depth for both
    features.
22. **"Длительность" splits movie vs series math** - a movie's `videos`/
    `media` entries are alternate versions of the same film (verified live on
    "Дюна: Часть вторая" - "24 fps"/"48 fps", KinoPub's own `duration.total`
    is their sum, ~5h33m for a ~2h47m movie), so a movie shows the first
    entry's own duration and never `duration`/`duration_average`. A series
    (`type` serial/docuserial/tvshow) shows both as intended: "одной серии ≈
    .../ , всего сериала: X дн. Y час. Z мин.", leading all-zero units
    dropped. `_item_details` now also exposes `duration_average`
    (`duration.average`).
21. **FFmpeg is now optional at build time** - `WITH_FFMPEG=0` in `.env`
    builds a backend image without it (900 MB -> 273 MB measured, and apt is
    not contacted at all). This closes the "FFmpeg removability" open
    question below. It only ever powered `/audio-hls/jobs` (the last rung of
    the audio ladder); everything else is untouched. `_ffmpeg_available()`
    requires both the flag *and* `shutil.which`, so a flag that lies cannot
    turn into a FileNotFoundError inside a background job. `/health` reports
    it, the player skips that rung locally instead of firing a doomed
    request, and Diagnostics shows a row. Changing it needs
    `docker compose up -d --build backend`, not a restart.
20. **"Похожие" section on the details card** (`v1/items/similar?id=`, the
    endpoint the user supplied). Real endpoint - 400 without `id`, 404 for a
    bogus one - but **empty for roughly two thirds of the catalogue**
    (measured: fresh serials 1/15, oldest serials 9/15; "Дом дракона" returns
    nothing even though kino.pub's own page shows a Похожие block for it, so
    the site fills that from something else). The block is built only when
    the answer is non-empty; no genre-based stand-in was invented to fill it.
19. **"Мои сериалы" showed 2 of 4** — and the cause was a wrong inference in
    item #18, not a coding slip. `v1/watching/serials?subscribed=1` is not the
    watchlist: the *endpoint* is titled "Список сериалов с новыми/не
    досмотренными сериями" in KinoPub's own doc index, and `subscribed` only
    narrows within that, so a finished subscription is absent entirely. No
    list endpoint exists (six paths tried, all 404; every widening param
    ignored). What does exist: **`v1/history` entries embed the whole item
    including `subscribed`/`in_watchlist`**, so `GET /catalog/watching/
    subscribed` assembles `subscribed=1` ∪ history-flagged serials, which is
    complete by construction. Reports `scanned_pages`/`history_exhausted`
    rather than hiding its scan depth. Lesson worth keeping: a parameter's
    documented meaning does not tell you the endpoint's domain.
18. **Four TV-reported UI fixes** (frontend only, no backend change):
    navigation no longer drags the focus ring to the first sidebar button
    (`route()` ended in `focusFirst()`, and the first `.focusable` in
    index.html is literally "Новинки" - cosmetic with a mouse, broken with a
    remote); the "Я смотрю" badge counts new *episodes* (`watching_new`)
    instead of list entries; "Я смотрю" gained kino.pub's "Все мои сериалы"
    toggle (both views come from the one `subscribed=1` payload - a sweep for
    a separate endpoint found only ignored params and 404s); and the details
    screen gained a "Серии:" pill row mirroring the season picker, watched
    filled green, part-watched outlined. Note for future work: `offsetParent`
    is useless as a visibility test under jsdom (no layout - everything reads
    hidden), so DOM-visibility checks that tests must exercise go by
    "no `.hidden` ancestor" instead.
17. **Filter panel rebuilt to look like kino.pub's own**, and the two rating
    ranges turned out to be real after all - `conditions[]` accepts
    `imdb_rating` and `kinopoisk_rating` (item #15 below says they do not
    exist; that was wrong, see CHANGELOG). Ratings step by whole numbers on
    purpose: KinoPub discards the decimal part of the bound. The sliders are
    remote-first (OK enters edit mode, OK swaps handle, arrows move, Back
    exits) because two handles on one rail cannot be told apart by `move()`.
    "Мне повезёт!" is wired to a real random pick inside the current filter.
16. **Direct/HDR: the app was declaring the TV incapable and KinoPub believed
    it.** See open item #1 — this is the real fix for the report item #14
    below only appeared to close. Capability probes now have an "unknown"
    state that never degrades into "no", `/device/capabilities` leaves
    unknown flags untouched, direct is no longer vetoed by one `canPlayType`
    string, and there is an explicit "Возможности устройства" setting plus a
    `GET /device/state` diagnostics row showing KinoPub's real flags.
15. **Filter panel: real fields only, year became a range, Anime genre bug fixed.**
    The stub panel (six `<select>` each with one "Любые" option, zero
    wiring) is now real: Жанр (`v1/genres?type=`, type-scoped per section —
    verified live), Страна (`v1/countries`), Год от/до (`conditions[]=
    year>=X`/`year<=X` — the encoding isn't documented anywhere, worked out
    and verified live), Период (`conditions[]=created>=<unix>` — "за
    неделю/месяц/год"), Качество (`quality=<reference id 1-4>`, **not** raw
    resolution — `quality=2160` silently returns nothing), Сортировка
    (`sort=field`/`-field`). Deliberately **not** added despite being on
    kino.watch's own panel: "Статус" (`finished` param is documented but
    does nothing — verified live, identical counts with/without),
    age/language/translation/voice-studio (no `v1/items` field for any of
    them, documented or otherwise; rating ranges were wrongly on this
    list too — they do exist, see item #17). Real bug found and fixed along
    the way: "Аниме" is `genre=25` under the hood (not a real `type`), and
    the filter panel's own genre picker was silently overwriting it —
    picking "Комедия" on the Anime page returned ordinary comedies, not
    anime comedies. Fixed on both ends: backend refuses to let an incoming
    `genre` override a section that already has one baked in, and the panel
    doesn't even offer a Genre picker on that section. Filter button itself
    is now hidden entirely on "Я смотрю" and "Спорт" (neither comes from
    `v1/items`, so it never did anything there either).
14. **An over-eager stall watchdog — a real bug, but *not* the HDR one it was
    blamed for.** `watchDirectStall()` really did have one fixed 12s deadline
    with no way to tell "stuck forever" from "slow but working", and now
    extends its window on any `progress` event (60s ceiling). Worth keeping.
    But it was diagnosed as the cause of the missing HDR on the strength of
    plausibility, not evidence, and it wasn't — see item #16. Kept in the map
    as a reminder of how that went: the symptom was re-reported unchanged
    after this shipped.
13. **"Закладки" folder-count/grid mismatch (real KinoPub duplicate, not a bug in extraction).**
    A folder said "5 тайтлов", grid showed 4. Root cause: KinoPub's own
    folder data really did contain the same title twice (verified live via
    `/bridge/explorer`), and `/catalog/bookmarks/{id}` was routing through
    the generic `extract_catalog_items()` (recursive tree-walk + id-dedupe,
    built for parsing-ambiguous responses like search) which silently
    dropped the second copy. Now builds directly from the folder's flat
    `items[]` via `normalize_catalog_item`, no dedup — matches what the
    real site shows.
12. **Pagination bar hidden when there's only one page.** Used to show a
    lone, permanently-active "1" button even for a 5-item bookmark folder.
11. **"Я смотрю" sidebar badge shows a count on page load, not just after
    opening the section** (`loadWatchingCount()` now runs once at
    `initializeAuthenticatedApp()`).
10. **"Закладки" built for real** (`v1/bookmarks`, `v1/bookmarks/view?
    folder=` — kinoapi.com/api_bookmarks.html). Folder list → click → same
    poster grid as everywhere else. Browsing only, no create/rename/delete
    (real documented endpoints for those exist, not wired up — not asked
    for).
9. **Quality selection overhaul** (biggest single piece of work this
   stretch — see CHANGELOG "Качество: фильм открывается в максимуме" for full
   detail): device capability flags (`support4k`/`supportHevc`/`supportHdr`)
   are now **reported to KinoPub from real browser probes**
   (`POST /device/capabilities`) instead of hardcoded `true` — this
   **directly disproved** an earlier session's conclusion that the 4K
   catalogue badge was "lying" (it wasn't; the device was declared
   non-4K-capable at the time, so KinoPub simply never offered 2160p
   files — proven by toggling the flags and re-reading `v1/items`). Also:
   every KinoPub HLS "quality" link for one title is the *same* master
   playlist listing all renditions (verified live) — the old quality
   selector was reloading an identical manifest, pure theatre; now it moves
   the hls.js level directly. `bestPlayableGroupIndex()`'s broken fallback
   (used to hand an HEVC-incapable device the *smallest*, still-undecodable
   variant) fixed to prefer the best decodable one instead.
8. **Native fullscreen escape-hatch button** in the player controls — jump
   to real `<video>`-element fullscreen (the HDR/hardware-video-plane path)
   at any point during playback, independent of the "Полный экран в
   плеере" setting. Also fixed a real double-toggle-pause bug: once
   `<video>` itself is the fullscreen element, the platform's own native
   fullscreen-video click gesture and this app's own click-to-pause handler
   both fired for one click.
7. **"Спорт" is real live TV channels** (`v1/tv`, `kinoapi.com/api_tv.html`)
   instead of a VOD genre filter that returned nothing. All 51 channels for
   this account are sport (ESPN, Eurosport, TNT Sport UHD, MATCH!...),
   playback via the existing `/hls` relay, no backend changes needed for
   the CDN.
6. **Device shows a real name/hardware/software in KinoPub's own device
   list** instead of "unknown"/"unknown"/"unknown" (`v1/device/notify`,
   `v1/device/{id}/settings`) — see item #9, these two pieces landed
   together.
5. **"Я смотрю" resolved for real**: `v1/watching/serials?subscribed=1` (the
   `subscribed=1` part was the whole fix — without it, it's "everything
   tracked with new episodes", not "what I marked Буду смотреть"). Green-eye
   toggle button on the serial details screen calls `v1/watching/
   togglewatchlist`.
4. **Watch-progress mirroring to KinoPub's real history/watched-status**
   (`v1/watching/marktime` + a *guarded* `v1/watching/toggle` — guarded
   because `toggle` flips rather than sets, and the player calls
   save-progress on both `pause` and `ended` for the same finish, so an
   unguarded toggle would flip an already-watched episode back off).
3. **Real 4K confirmed genuinely absent from most of the catalogue at the
   time** (~46 titles checked, 0 had an actual 2160p file) — this
   conclusion was later **proven wrong** by item #9 above; it was a device-
   capability artifact, not a catalogue-quality lie. Left in the map for
   context; don't re-derive it as if it's still true.
2. **Filter/quality/HDR items above are the bulk of this stretch.** Earlier
   items (#1-8 in prior handoffs — 401 handling, catalogue pagination,
   history dates, episode carousel, vote buttons, browser back/forward,
   etc.) are stable and covered in CHANGELOG's earlier entries; not restated
   here.

## Known gaps / flagged but not done

- **Settings that don't do anything**: `audio_language` and `autoplay_next`
  are saved and never read. `reduce_motion` toggles a CSS class that doesn't
  exist in `styles.css`.
- **Search falls back to a mock catalogue on *any* error**, not just
  KinoPub-unreachable — `KPApi.search` in `api.js`.
- **Progress/"continue watching" for the *button*, not the episode-card
  marks, is still local-only.** Episode-card watched marks use real
  `v1/watching` data; the Continue button's own resume position still only
  reads this bridge's local SQLite, never KinoPub's own cross-device
  position.
- ~~FFmpeg removability still an open question.~~ **Resolved** - see item #21; `WITH_FFMPEG=0` in `.env` builds without it (currently how the running backend is built — see open item #1 at the top).
- ~~`v1/collections` (real curated collections/подборки, "Подборки" sidebar
  button still dead) — confirmed-real, unused endpoint. Not requested yet.~~
  **Resolved** — see item #23; "Подборки" is wired for real.
- ~~"Новые эпизоды" sidebar link still dead~~ **Resolved** — see item #25;
  removed outright (confirmed live, no API equivalent exists).
- **HDR real-TV confirmation** — see open item #2 at the top.
- **backend/app/__pycache__/main.cpython-311.pyc is tracked in git** and
  changes on every `py_compile`. Should be gitignored; flagged to the user,
  not yet addressed as of this handoff.

## Testing infrastructure

**Frontend**: `frontend/tests/`, eight plain Node scripts, no framework —
`frontend/app.js` is a single IIFE with no module boundary, so each test
file `eval`s the real source with the closing `}());` swapped to also expose
functions under test on `global.__app`. Four use hand-rolled DOM stubs (fast,
no deps: `harness.js`, `subs.js`, `misc.js`, `quality.js`); four
(`sections.js`, `actions.js`, `episodes.js`, `panel.js`) load the real
`index.html` into `jsdom`. See `frontend/tests/README.md` for the per-file
breakdown. **312 checks, 0 failures** as of backend 0.9.97 (this count moves
every session — check `frontend/tests/README.md`'s own header for the true
current number rather than trusting this one long-term).

Run via PowerShell (Bash mangles Windows paths for `docker run`):
```powershell
docker run --rm -v "D:\pets\kinopub-webos-client\frontend:/f:ro" -w /tmp node:20-alpine sh -c "cp -r /f /tmp/frontend && cd /tmp/frontend/tests && npm install --silent >/dev/null 2>&1 && node harness.js ../app.js && node subs.js ../app.js && node misc.js ../app.js && node quality.js ../app.js && node sections.js ../app.js && node actions.js ../app.js && node episodes.js ../app.js && node panel.js ../app.js"
```
**`npm install` for the four jsdom-based suites is intermittently very slow
in this sandbox** (can look hung for minutes). It does succeed — run it via
a background PowerShell call rather than assuming the registry is down and
giving up; a `runtests.sh` mounted into the container and run in the
background worked reliably this session (per-file pass/fail counts,
survives npm being slow).

Also run `node --check app.js` and, ideally, `npx eslint --no-eslintrc --env
browser,es2020 --parser-options=ecmaVersion:2020 --rule
'{"no-undef":"error"}' app.js` after any change.

**Backend**: `backend/smoke_test.py`. Boots the real FastAPI app with a
seeded SQLite session, hits every endpoint that doesn't need live KinoPub.
**36 checks, 0 failures** as of backend 0.9.97 (same caveat as the frontend
count above — check the actual run output, this number only moves forward).
Run via PowerShell:
```powershell
docker run --rm -v "D:\pets\kinopub-webos-client\backend:/src:ro" -w /app kinopub-webos-client-backend sh -c "cp /src/smoke_test.py . && rm -rf /app/app && cp -r /src/app /app/app && python -m py_compile app/main.py && python smoke_test.py"
```
Then, if it passed: `docker compose up -d --build backend` (PowerShell),
then `curl http://localhost:8080/bridge/health` to confirm the version
string actually moved.

**Verifying live mutations (POST/PUT endpoints) that `/bridge/explorer`
can't reach (GET-only)**: write a throwaway Python script using `httpx`
against `api.service-kp.com` directly, reading the real access token out of
the backend's own sqlite (`sessions` table, `/data/kp.db` inside the
container) or — more reliably this session — driving it from the **already-
authenticated browser tab** via `fetch('/bridge/api/v1/...', {method:
'POST', credentials:'include', ...})`, since `/bridge/api/{path}` is a raw
authenticated passthrough to any `v1/` path, any method. Always toggle
mutating test calls back to their original state afterward (this session
did this for device settings, watchlist toggle, and a bookmark's watch
status) — this is a **real user account**, not a sandbox.

**Both are ad-hoc verification scripts that accumulated over many turns, not
a designed test suite.** Extend in the same spirit rather than replace with
a framework, unless asked.

## If you're picking this up cold, do this first

1. **Read the two open items at the very top of this file.** Neither is
   fully blocking: item #1 is a one-question check-in with the user about
   whether `WITH_FFMPEG=0` in `.env` was meant to stick; item #2 (HDR) needs
   the user's real-TV confirmation before it can be called done.
2. `cd D:/pets/kinopub-webos-client && git log --oneline -5` and
   `curl http://localhost:8080/bridge/health` — confirm what's actually
   running vs. what's in git. Expect backend `0.9.97`, `"ffmpeg": false`
   (see open item #1 — that's the current, possibly-unintended, state).
3. Skim `CHANGELOG.md` top-to-bottom (newest-first) for full prose behind any
   bullet in the "Everything fixed" map above.
4. If touching `frontend/app.js`: run the suite in `frontend/tests/` before
   and after (see Testing above). Most regressions across this whole
   project's history have been single-string-replace mistakes or a stale
   variable captured across an async boundary, not logic errors the tests
   miss.
5. If touching `backend/app/main.py`: `py_compile`, then
   `backend/smoke_test.py`, then bump the version string and rebuild via
   PowerShell (`docker compose up -d --build backend`) — verify
   `/bridge/health` actually reports the new version before calling it done.
6. **When an API shape is uncertain, verify it live before writing code
   against it** — `/bridge/explorer` for GET, a throwaway authenticated
   `fetch`/`httpx` call for mutations (see Testing above). This is the
   single most consistent lesson across this entire project's history:
   every real bug fixed this session, and every "this feature genuinely
   isn't possible" conclusion, came from checking first, not from a
   plausible-looking guess.
