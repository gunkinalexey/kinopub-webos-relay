# Handoff — kinopub-webos-client

Written 2026-08-14 to continue this work in a fresh context window. Read
this first, then `CHANGELOG.md` (full changelog, newest first) if you need
detail on a specific past change.

## Open items — check these before anything else

**Nothing blocking, nothing mid-conversation.** Working tree is clean, `main`
is pushed and matches what's deployed. One thing worth knowing on pickup:

1. **Real-TV confirmation of the double-fetch fix (v0.13.1) is still
   informal.** It was verified on `kinopub.lan` via `fetch()` instrumentation
   in a real (non-sandbox) browser tab with an authenticated session — see
   CHANGELOG v0.13.1 for the exact method and why the first two attempts at
   verifying it were themselves broken (jsdom `location` aliasing, mocked
   API resolving faster than a real network call). Solid evidence, but
   nobody has watched it on the actual webOS TV yet.

## What this project is

A lightweight KinoPub web client for LG webOS TV browsers (and desktop as a
secondary target). FastAPI backend (`backend/app/main.py`, one file) bridges
the KinoPub API — auth, catalogue, streaming, image proxy, CDN server
picker, self-update — to a vanilla-JS frontend (`frontend/app.js`, one file,
one closure, no build step, no framework). `frontend/index.html` +
`styles.css` round it out. `docker-compose.yml` runs both: backend built
from `backend/Dockerfile`, frontend is `nginx:1.27-alpine` bind-mounting
`./frontend` read-only — **frontend changes are live immediately, no
rebuild**.

Backend version string lives at `app = FastAPI(..., version='0.9.NN', ...)`
in `main.py` near the top; bump it whenever `backend/` changes so `/health`
reflects what's actually deployed. **Currently: backend 0.9.101.**

The real upstream API is `https://api.service-kp.com` (`API_BASE`). It is
**intermittently unreachable** — this happened multiple times this session,
confirmed as a genuine TCP-connect-level outage (`socket.connect()` timing
out, not an app bug) by testing from both the container and the host
directly. Don't read a live 502/504 as something broken in this codebase
without checking connectivity first. The built-in `GET
/bridge/explorer?path=&query=` endpoint (GET-only, real-account, redacts
tokens) is the fastest way to ground-truth a real endpoint. It cannot POST;
mutating endpoints need a throwaway script run inside the backend container,
or `fetch('/bridge/api/v1/...', {credentials:'include', method:'POST'})`
from an already-authenticated browser tab (`/bridge/api/{path}` is a raw
authenticated passthrough to any `v1/` path, any method).

## Current state

- **Branch: `main`.** (The old `rework/audio-subtitles-details` branch this
  handoff used to point to is gone from the picture — the repo was moved to
  a private GitHub remote and everything since has gone straight to `main`.)
- Remote: `https://github.com/gunkinalexey/kinopub-webos-relay.git`
  (private). `git push`/`pull` need a working credential — deploy key on the
  Proxmox side, whatever's configured locally on Windows.
- Backend version in the repo: **0.9.101**; still **0.9.99 on `kinopub.lan`**
  — the HLS-recovery fix and the refactor below it are committed nowhere yet,
  so the TV is running neither. `curl http://localhost:8080/bridge/health`
  (local) or `curl -k https://kinopub.lan/bridge/health` (deployed, Caddy
  redirects http → https) to check.
- **340 frontend checks, 0 failures.** Backend smoke tests also green (see
  Testing below).
- **Deployed on Proxmox, this is the primary/real instance now**, not just a
  dev convenience:
  - LXC `kinopub` (ID 120, `192.168.0.50`) runs the compose stack from
    `/opt/kinopub`.
  - LXC `edge` (ID 110, `192.168.0.40`) runs Caddy (native systemd install,
    not Docker) and serves the app at `http://kinopub.lan`.
  - DNS: a Keenetic router `ip host` entry points `kinopub.lan` at the edge
    LXC's IP. Keenetic has no wildcard support, so a new service needs its
    own `ip host` line plus a `Caddyfile` block.
  - The Windows checkout (`D:\pets\kinopub-webos-client`) is for development
    and local testing only — `docker compose up`/`down` there doesn't touch
    the real deployment, which only moves via `git push` + the auto-updater
    or a manual `git pull` on the Proxmox side.
  - Full deploy/proxy setup instructions are in `README.md` (rewritten this
    session into a three-step walkthrough: local → Proxmox → `kinopub.lan`).
- **Self-update system is live and working** (v0.13.0–v0.13.3, see
  CHANGELOG): a Settings-screen button checks GitHub and applies updates.
  Architecture and gotchas below under "Auto-update system".

## How work has been happening (read this before doing anything)

**Commit deliberately, at the end of a logical change.** There used to be a
`Stop` hook that ran `git add -A` and committed after every assistant turn;
it was removed for a concrete reason — a working-database export left in
the repo directory (`kp-transfer.db`, containing KinoPub `access_token` and
`refresh_token`) was swept into a commit and pushed to GitHub before anyone
looked. Purging it needed a history rewrite (`git filter-repo` + force-push)
and a device revocation on the real account. Blanket `git add -A` on a timer
is how that happens — stage what you actually changed, review `git status`
before committing.

- **`CHANGELOG.md` is the changelog** (`README.md` is the
  project/deployment doc — see its new three-step structure), written in
  Russian, newest entry first, one section per user-visible change: a
  `## vX.Y.Z — <short title>` heading (or `## backend 0.9.NN — ...` when
  only the backend version moved), then prose explaining *why*, not just
  what. Recent entries go well beyond a one-liner when the story matters —
  see v0.13.1 (a two-paragraph account of a verification script that was
  itself broken twice) as the current bar for "when it's worth the space."
- Rebuild after backend changes: `docker compose up -d --build backend`
  (cached, fast). `rebuid_containers.bat` (typo is original) is NOT that —
  it does `build --no-cache` and ends in `pause` (hangs forever headless).
  Kept only for manual full rebuilds.
- **Never put database dumps, `.env` copies, or anything with credentials
  inside the repo directory.** `*.db` and `update/` are gitignored, but the
  safe habit is keeping such files entirely outside the checkout — see the
  `Stop`-hook incident above, which happened despite the gitignore rule
  existing at the time.
- `ignore/` (note: **not** `.gitignore` — a literal directory named
  `ignore/`) is itself gitignored wholesale and is where ad-hoc verification
  scripts live now: `ignore/verify-hash-dedup.js`, `ignore/verify-update-dates.js`,
  `ignore/resize.py` (icon prep — see "Assets" below), `ignore/mdcheck.py`
  (checks README code-block hygiene). None of these ship; they're kept
  locally as reusable regression checks with the specific gotchas they
  exist to catch documented in their own header comments. Worth reading
  before writing a new one — several already document a mistake ("this
  looked like it tested the bug but actually tested nothing") so it isn't
  repeated.
- `examples/` (kino.watch saved pages, ground-truth for real page layouts)
  is gitignored — each saved page carries a live `csrf-token`. Never
  `git add -f` it.

**Docker on this machine (Windows)**: intermittently the `docker` CLI can't
reach the daemon (`Docker Desktop` app not running) — check with `docker
version` before assuming a command failure is a code problem. Use
**PowerShell** for `docker`/`docker compose` calls (Bash mangles Windows
paths for `docker run` volume mounts). Bash is fine for `curl`, `grep`,
`git`, reading files, and running things *inside* an already-started
container via `docker exec`.

**Line endings matter for anything that runs on the Linux boxes**
(`deploy/updater.sh`, in principle any future shell script). `git config
core.autocrlf` is `true` on this Windows checkout, which converts LF→CRLF on
checkout but is *supposed* to convert CRLF→LF on commit — verify what's
actually in the object database with `git show :path | python -c
"import sys;print(sys.stdin.buffer.read().count(b'\r'))"` rather than
trusting the `warning: LF will be replaced by CRLF` message, which is about
checkout direction only and fired (harmlessly) on every commit this session
even when the stored blob was clean LF. A `\r` before the shebang's
interpreter name genuinely breaks the script on Linux
(`env: 'bash\r': No such file or directory`).

**Git file mode (executable bit) is a real, separate failure class from
line endings — bit the auto-updater in production this session.**
`deploy/updater.sh` was committed as `100644` (no +x), while the README's
own install instructions said to `chmod +x` it. That chmod creates a
git-visible "modified" diff (mode only, zero content lines) that
**permanently blocks `git pull --ff-only`** on that install — not once, but
on every future pull, because the local working-tree mode never matches
whatever HEAD says until the file is re-committed with the mode the
installed copy already has. Symptom looked confusing at first: `git status`
showed a real diff, but `git diff <file>` showed only `old mode 100644 / new
mode 100755` with no content changes, which was the tell. Fixed by
committing the file as `100755` (`git update-index --chmod=+x <path>` before
committing) — but the *existing* broken install still needed manual
intervention (`git checkout -- <path>` to discard the local mode diff, then
`git pull`) because git won't fast-forward a path that already differs from
HEAD, even when the divergence would resolve itself. If a future script
needs `chmod +x` in its own install instructions, commit it executable from
the start; don't rely on the installer's `chmod` to be harmless.

**The user communicates in Russian** and expects responses in Russian.
Technical, notices when something is hand-wavy, and routinely catches real
bugs from a single terse observation. The consistent pattern across this
whole project: **reproduce the symptom live before proposing a fix, verify
the fix live afterward, don't call something done from test-suite green
alone if a live system is available to check against.** This session, that
discipline caught two things that would otherwise have shipped broken:

- A verification script for the hash-dedup bug (v0.13.1) that "passed" on
  both the buggy and the fixed code — because the jsdom test harness lacked
  `global.location = window.location`, so `pushHash()` never actually wrote
  the hash and the bug path never ran either way. A green test that isn't
  exercising the thing it claims to test is worse than no test — it was
  caught by deliberately re-running the same check against the pre-fix
  commit and noticing it *also* passed.
- The auto-updater's own dates feature (v0.13.2/v0.13.3) shipped, was
  applied to the live server, and the UI still showed no dates — because
  the running `updater.sh` at that exact moment was still executing its
  *pre-update* self (a self-updating script can't hot-reload its own
  function bodies mid-run), so the very update that should have proven the
  feature worked was, itself, incapable of demonstrating it. Required a
  second cycle plus a bootstrap-seed fix (v0.13.3) to actually show
  anything on a fresh install. Caught by checking the live server after
  the "done" message, not by trusting the commit.

**Real endpoints are never guessed into existence.** Confirm via
`/bridge/explorer` or docs before writing code against a shape. This has
burned time repeatedly in the project's history (guessed paths that looked
right and weren't).

**Never ship a filter/control that looks like it works but doesn't.**
Several "obviously should exist" controls turned out to have no real
backing API field, confirmed live — the policy has been: don't fake it,
don't add the UI control, say so in a comment/changelog entry instead.

## This session's work (2026-08-14, one long session)

Roughly in order; see CHANGELOG for full prose on each:

1. **киноТёрк icon folder → WebP.** Icons moved from hardcoded PNG filenames
   to "whatever's in `frontend/assets/dog/`" (nginx JSON autoindex,
   `app.js` reads the listing). Then converted PNG→WebP (36MB→4.6MB per
   batch) because they're photos with real transparency (JPEG has no alpha
   channel — tried it first, was wrong). `ignore/resize.py` does the
   resize+convert now; rerun it after dropping new source photos in
   `ignore/dog/`.
2. **Repo history rewrite**: a `rufus-4.15p.exe` and ~250MB of superseded
   PNG icon history got into git over time. `git filter-repo` cut it to
   ~5MB, force-pushed. A `git bundle` backup was taken first (kept outside
   the repo).
3. **Backend performance pass**: server-side image cache (LRU, ETag/304),
   catalogue-list cache, page-count cache persisted across restarts,
   genre/country reference cache, a fixed poster cache-busting bug
   (`cacheVersion` was regenerating every page load, defeating the
   `immutable` header). ~100x speedup on cached image requests, measured.
4. **`STREAM_HOST_SUFFIXES` misconfiguration found and fixed**: `.env` had
   the literal example line from the comment (`example-cdn.net`) instead of
   real hosts, silently breaking Relay/HLS and all image proxying while
   Direct kept working (it bypasses the proxy) — which is exactly the kind
   of asymmetry that makes this bug hard to notice. Real hosts
   (`staticpop.net`, `cdntogo.net`) now in both `.env` and `.env.example`.
5. **CDN server picker** (`GET /servers`, `POST /servers/select`,
   `POST /servers/measure`): measures latency+throughput per KinoPub CDN
   region by actually switching to each and downloading a sample, then
   applies (or reports) the fastest. Real account device-settings mutation
   under a lock, with rollback in a `finally`.
6. **Dead session no longer stuck for 24h**: `refresh_if_needed` only
   checked the clock; a token KinoPub revoked early (observed live) left
   the app dead until the stored expiry passed. Now retries on any real 401
   from `kino_get`/`kino_post`.
7. **Subtitle language codes**: table only knew 14 languages, fixed to 214
   (both ISO 639-2 /T and /B spellings — KinoPub is inconsistent about
   which it sends, confirmed live). Also identified `lang:"ai"` as
   machine-generated-subtitle marker (not a real ISO code) by downloading
   and reading three actual files tagged with it.
8. **Homelab reverse-proxy deployment** (see "Current state" above for the
   resulting topology): Caddy on a separate LXC, Keenetic DNS, and the whole
   Proxmox LXC creation dance including two gotchas worth remembering for
   next time —
   - `pveam download` needs the exact template filename with **correct
     architecture** (`amd64` vs `arm64`); picking the wrong one creates a
     container that fails silently at `pct start` with an error that
     doesn't mention architecture at all.
   - Debian 13 (systemd 257) needs `nesting=1` in an unprivileged LXC even
     without Docker involved — Proxmox does warn about this at `pct create`
     time, but the warning is easy to miss.
9. **README rewritten** from a device-log-style page into a three-step
   walkthrough (local Docker Desktop → Proxmox LXC → `kinopub.lan` via
   Caddy), each step self-contained and independently stoppable. One
   command per code block (no `&&`-chained multi-purpose lines) per explicit
   user request.
10. **Auto-update system** (v0.13.0–v0.13.3): `deploy/updater.sh` runs on
    the Proxmox host under a systemd timer (every 5 min), diffs which paths
    changed between the current commit and `origin/main`, and does the
    *minimum* necessary — nothing at all if only `frontend/`/docs changed
    (bind-mounted, picks itself up), a container recreate for
    `docker-compose.yml`/`nginx.conf`, a full rebuild only if `backend/`
    changed, with an automatic rollback (`git reset --hard` + rebuild) if
    the post-update health check fails. Talks to the app over two files in
    a shared `update/` directory (`status.json` it writes, `requested` the
    UI creates) — deliberately not `docker.sock`, which would be
    root-on-the-host for a service that proxies internet content.
    `last_applied_at`/`current_committed_at`/`remote_committed_at` show in
    the Settings-screen "Обновление" block. See item above ("this session's
    dates bug") for the self-hosting bootstrap trap this hit in production.
11. **Catalogue double-fetch fixed, pagination capped to page 1** (v0.13.1).
    `route()` renders synchronously *and* writes `location.hash`; the
    resulting `hashchange` fired `applyHash()` → `route()` again a tick
    later, doubling every `/catalog/list` call on every section switch —
    confirmed live via network log on `kinopub.lan` (two identical requests
    back-to-back). Fixed by tagging the self-written hash and having the
    `hashchange` handler consume it once. Pagination removed from the main
    catalogue/filter view specifically (not the shared `renderPagination()`,
    which History/Bookmarks/Collections still use with their own tests) —
    user request, "cut the listing to page 1."
12. **`watch_progress` local table removed** — it duplicated what KinoPub's
    own `v1/watching` already returns (verified live on 6 titles, positions
    matched to the second), and the two sources had already drifted once
    (a stale local row without `episode_number` never mirrored upstream).
    `/history` now reads straight from `v1/watching`.

Older work (pre-2026-08-14, ~0.9.79→0.9.97, subtitle/audio track selection,
HDR/device-capability fixes, filter panel, collections, history) is stable
and fully covered in `CHANGELOG.md`; not restated here. If you need that
context, `CHANGELOG.md` newest-first has it — nothing there needs
re-verifying, it was each verified live at the time.

## Known gaps / flagged but not done

- **Settings that don't do anything**: `audio_language` and `autoplay_next`
  are saved and never read. `reduce_motion` toggles a CSS class that may not
  exist in `styles.css` — worth a re-check, not confirmed this session.
- **Progress/"continue watching" via the *button*** now reads real
  `v1/watching` data (see item #12 above) — this line in prior handoffs is
  resolved, kept here only so nobody re-investigates it as if it's still
  local-only.
- **Dead SVG sprite symbols**: `i-tv`, `i-subtitles`, `i-wifi`, `i-speed`,
  `i-chat`, `i-mail`, `i-ruble`, `i-send` are declared in `index.html`'s
  sprite but never referenced — leftovers from unshipped screens. Found
  during a code-quality pass, not removed (low priority, zero user impact).
- **HDR real-TV confirmation** — see "Open items" #1 at the top for the
  *current* live-verification gap (double-fetch fix); the original HDR
  device-capability fix from early August was already confirmed resolved
  in a subsequent session per the old handoff and is not re-flagged here.

## Assets

`frontend/assets/dog/` — киноТёрк branding icons, WebP, picked up from
whatever's in the folder at request time (nginx JSON autoindex + `app.js`
listing fetch, see item #1 above). To add more:
1. Drop source photos (any format) in `ignore/dog/`.
2. `python ignore/resize.py` — resizes to 648px on the short side (matches
   the largest on-screen use, the hover preview) and re-encodes as WebP.
3. Old same-name files in another extension get deleted automatically by
   the script (avoids showing the same photo twice under two names).

## Testing infrastructure

**Frontend**: `frontend/tests/`, eight plain Node scripts, no framework —
`frontend/app.js` is a single IIFE with no module boundary, so each test
file `eval`s the real source with the closing `}());` swapped to also
expose functions under test on `global.__app`. Four use hand-rolled DOM
stubs (`harness.js`, `subs.js`, `misc.js`, `quality.js`); four
(`sections.js`, `actions.js`, `episodes.js`, `panel.js`) load the real
`index.html` into real `jsdom`. **312 checks, 0 failures** as of this
handoff (recheck the actual run — this number moves).

Four of the eight scripts take the `app.js` path as `argv[2]`; without it
they crash before printing a single PASS/FAIL line, which reads as "0/0,
probably fine" if you're not watching closely — always pass it explicitly:

```powershell
$sp = "<some scratch dir with runtests.sh — see below>"
docker run --rm -v "D:\pets\kinopub-webos-client:/app" -v "${sp}:/scratch" node:20-alpine sh /scratch/runtests.sh
```

where `runtests.sh` is:
```bash
cd /app/frontend/tests
npm install --silent --no-audit --no-fund >/dev/null 2>&1
for f in harness actions episodes misc panel quality sections subs; do
  node "$f.js" ../app.js
done
```
(`npm install` for the jsdom-based suites can look hung for a while in this
sandbox — it does finish; let it run rather than assuming the registry is
down.)

**Backend**: `backend/smoke_test.py`. Boots the real FastAPI app with a
seeded SQLite session, hits every endpoint that doesn't need live KinoPub.
It is **not** copied into the Docker image (`Dockerfile` only `COPY`s
`app/`), so copy it in before running:
```powershell
docker cp backend/smoke_test.py <backend-container>:/app/smoke_test.py
docker compose exec -T backend python smoke_test.py
```

**Verifying live mutations** (device settings, server-location switch,
watchlist toggle, etc.) that `/bridge/explorer` can't reach (GET-only): a
throwaway Python `httpx` script run inside the backend container (reads the
real access token from `/data/kp.db`'s `sessions` table), or a `fetch()`
from an already-authenticated browser tab via the `/bridge/api/{path}`
passthrough. **Always toggle test mutations back to their original state
afterward — this is a real account, not a sandbox.** The CDN
server-location switch (item #5 above) and the device-capability probes
were both restored after testing this session.

**For anything involving `location.hash`/`hashchange` specifically**: a
plain jsdom harness with only `global.window`/`global.document` set is not
enough — see "How work has been happening" above for exactly what's
missing (`global.location`) and why a mocked API resolving synchronously
also silently defeats this class of test. `ignore/verify-hash-dedup.js` has
the working pattern with both gotchas documented inline; copy its setup
rather than re-deriving it.

## If you're picking this up cold, do this first

1. `cd D:/pets/kinopub-webos-client && git log --oneline -5` and
   `curl http://kinopub.lan/bridge/health` — confirm what's actually
   deployed vs. what's in git. Expect backend `0.9.99`, and `git status`
   clean on `main`.
2. Skim `CHANGELOG.md` top-to-bottom (newest-first) for full prose behind
   anything in "This session's work" above.
3. If touching `frontend/app.js`: run the suite in `frontend/tests/` before
   and after (see Testing above). Most regressions across this project's
   history have been single-string-replace mistakes or a stale variable
   captured across an async boundary, not logic errors the tests miss —
   but see this session's own two near-misses (in "How work has been
   happening") for what a test that *looks* like it covers something but
   doesn't looks like in practice.
4. If touching `backend/app/main.py`: `smoke_test.py`, then bump the
   version string, rebuild (`docker compose up -d --build backend`
   via PowerShell), and confirm `/health` actually reports the new version.
5. If touching `deploy/updater.sh` or anything else that runs directly on
   the Proxmox boxes: check line endings and file mode before committing
   (see "How work has been happening" above) — both bit this session in
   ways that only surfaced on the real Linux host, never locally.
6. **When an API shape is uncertain, verify it live before writing code
   against it.** This is the single most consistent lesson across this
   entire project's history, restated again this session by two separate
   near-misses where "the test passed" was not the same thing as "the fix
   works."
