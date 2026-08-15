"""Exercises the endpoints that need no upstream call, with a real session row.

Catches what syntax checks and pyflakes cannot: a response that does not match
its own return annotation. FastAPI validates responses against that annotation,
so `-> Dict[str, bool]` on a handler returning a number turns every call into a
500 — which is how a page reload once looked like a lost login.

Run:  docker compose exec -T backend python smoke_test.py
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import time

DB = os.path.join(tempfile.mkdtemp(), 'smoke.db')
os.environ['DB_PATH'] = DB
os.environ.setdefault('KINOPUB_CLIENT_ID', 'smoke')
os.environ.setdefault('KINOPUB_CLIENT_SECRET', 'smoke')

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

SID = 'smoke-session'
failures = []


def check(name, condition, detail=''):
    print(f'{"PASS" if condition else "FAIL"}  {name}' + (f'\n        {detail}' if not condition else ''))
    if not condition:
        failures.append(name)


# raise_server_exceptions=False so a 500 is reported as a failed check instead
# of aborting the run at the first broken endpoint.
with TestClient(app, raise_server_exceptions=False) as client:
    # The lifespan created the schema; seed a session that looks authenticated.
    conn = sqlite3.connect(DB)
    conn.execute(
        'INSERT INTO sessions(sid, access_token, refresh_token, expires_at, updated_at) VALUES (?,?,?,?,?)',
        (SID, 'access', 'refresh', time.time() + 86400, time.time()))
    conn.commit()
    conn.close()

    r = client.get('/health')
    check('GET /health', r.status_code == 200, f'HTTP {r.status_code} {r.text[:120]}')

    r = client.get('/auth/status')
    check('GET /auth/status without a cookie', r.status_code == 200, f'HTTP {r.status_code} {r.text[:120]}')
    check('  reports unauthenticated', r.json().get('authenticated') is False, r.text[:120])

    client.cookies.set('kp_session', SID)
    r = client.get('/auth/status')
    check('GET /auth/status with a session', r.status_code == 200, f'HTTP {r.status_code} {r.text[:200]}')
    body = r.json() if r.status_code == 200 else {}
    check('  reports authenticated', body.get('authenticated') is True, str(body)[:160])
    check('  refresh token flag survives the response model',
          body.get('has_refresh_token') is True, str(body)[:160])
    check('  expires_in stays a number, not coerced to bool',
          isinstance(body.get('expires_in'), int) and body['expires_in'] > 3600, str(body)[:160])

    r = client.get('/settings')
    check('GET /settings', r.status_code == 200, f'HTTP {r.status_code} {r.text[:120]}')
    check('  player_fullscreen present', 'player_fullscreen' in r.json(), r.text[:160])

    check('  device_profile present', 'device_profile' in r.json(), r.text[:200])

    r = client.put('/settings', json={'quality': 'auto', 'subtitle_size': 125})
    check('PUT /settings', r.status_code == 200, f'HTTP {r.status_code} {r.text[:120]}')

    r = client.put('/settings', json={'device_profile': 'tv'})
    check('PUT /settings keeps device_profile', r.json().get('device_profile') == 'tv', r.text[:200])

    # The whole point of the tri-state payload: a field the browser could not
    # determine must be allowed through as null and never coerced to False,
    # because False is what KinoPub writes as `supportHevc=0` - which strips
    # the HEVC/HDR file list for every title on the account.
    from app.main import CapabilitiesPayload
    blank = CapabilitiesPayload()
    check('capabilities default to "unknown", not "unsupported"',
          (blank.hevc, blank.uhd, blank.hdr) == (None, None, None),
          f'{blank!r}')
    partial = CapabilitiesPayload(hdr=True)
    check('a single known flag leaves the others unknown',
          (partial.hevc, partial.uhd, partial.hdr) == (None, None, True),
          f'{partial!r}')

    # The filter panel's sliders are useless if the query params silently are
    # not wired up - a typo here would just be ignored by FastAPI and the
    # catalogue would come back unfiltered, which looks like a working
    # control that does nothing. The schema is the cheap way to catch it
    # without an upstream call.
    schema = client.get('/openapi.json').json()
    params = {p['name'] for p in schema['paths']['/catalog/list']['get']['parameters']}
    check('/catalog/list takes the rating-range params',
          {'imdb_from', 'imdb_to', 'kp_from', 'kp_to'} <= params,
          sorted(params))
    check('/catalog/list still takes the year range', {'year_from', 'year_to'} <= params, sorted(params))

    check('/catalog/watching/subscribed is wired',
          '/catalog/watching/subscribed' in schema['paths'], sorted(schema['paths'])[:12])
    check('/catalog/items/{item_id}/similar is wired',
          '/catalog/items/{item_id}/similar' in schema['paths'], sorted(schema['paths'])[:12])
    check('/catalog/collections is wired', '/catalog/collections' in schema['paths'], sorted(schema['paths'])[:12])
    check('/catalog/collections/{collection_id} is wired',
          '/catalog/collections/{collection_id}' in schema['paths'], sorted(schema['paths'])[:12])

    # WITH_FFMPEG=0 builds an image with no ffmpeg at all, so the player has to
    # be able to ask before offering the remux. And the flag alone must never
    # be believed: claiming a binary that is not installed would turn a clear
    # refusal into a FileNotFoundError inside a background job.
    from app.main import _ffmpeg_available
    check('/health reports whether FFmpeg is in this build',
          isinstance(client.get('/health').json().get('ffmpeg'), bool),
          client.get('/health').text[:160])
    for value in ('0', 'false', 'no', 'off', ''):
        os.environ['WITH_FFMPEG'] = value
        if _ffmpeg_available():
            check(f'WITH_FFMPEG={value!r} disables the remux', False, 'still reported available')
            break
    else:
        check('every "off" spelling of WITH_FFMPEG disables the remux', True, '')
    os.environ['WITH_FFMPEG'] = '1'
    check('WITH_FFMPEG=1 still needs the binaries to actually be there',
          _ffmpeg_available() == bool(shutil.which('ffmpeg') and shutil.which('ffprobe')), '')

    r = client.get('/history')
    check('GET /history (local progress)', r.status_code == 200, f'HTTP {r.status_code} {r.text[:120]}')

    r = client.put('/history', json={'media_id': 'x', 'position': 10, 'duration': 100})
    check('PUT /history', r.status_code == 200, f'HTTP {r.status_code} {r.text[:120]}')

    r = client.get('/debug/events')
    check('GET /debug/events', r.status_code == 200, f'HTTP {r.status_code} {r.text[:120]}')

    r = client.post('/debug/events', json={'message': 'smoke'})
    check('POST /debug/events', r.status_code == 200, f'HTTP {r.status_code} {r.text[:120]}')

    # /mock/search and its five hardcoded titles are gone on purpose: the
    # frontend used to fall back to them whenever the real search failed, so
    # an auth or network problem quietly produced five unplayable fake films
    # that looked like results. A failed search now surfaces the failure.
    r = client.get('/mock/search?q=')
    check('GET /mock/search is gone', r.status_code == 404, f'HTTP {r.status_code} {r.text[:120]}')

    # "Актёры"/"Режиссёры" search modes silently ran the same all-fields
    # query as "Все" until this fix - v1/items/search's real `field` param
    # was never sent. This locks in the mapping, not the live upstream
    # result (no network in this suite).
    from app.main import SEARCH_MODE_FIELDS
    check('search mode -> real v1/items/search `field` value',
          SEARCH_MODE_FIELDS == {'title': 'title', 'actor': 'cast', 'director': 'director'},
          SEARCH_MODE_FIELDS)
    check('"Все" sends no field param (searches every field, as documented)',
          SEARCH_MODE_FIELDS.get('all') is None, SEARCH_MODE_FIELDS.get('all'))

    # The details screen's clickable genre/country badges only link to a real
    # filter when KinoPub's payload actually carried an id - an id-less entry
    # must read as `id: None`, never a guessed/invented one.
    from app.main import _id_name_list
    check('id+title pairs pass through',
          _id_name_list([{'id': 2, 'title': 'Боевик'}]) == [{'id': 2, 'title': 'Боевик'}],
          _id_name_list([{'id': 2, 'title': 'Боевик'}]))
    check('a plain string list (no id in the payload) gets id:None, not a fabricated one',
          _id_name_list(['Спортивные']) == [{'id': None, 'title': 'Спортивные'}],
          _id_name_list(['Спортивные']))
    check('a comma-separated string (older payload shape) is split the same way',
          _id_name_list('США, Канада') == [{'id': None, 'title': 'США'}, {'id': None, 'title': 'Канада'}],
          _id_name_list('США, Канада'))

    # Bad input must be rejected cleanly, not blow up.
    r = client.get('/catalog/list?section=nope&feed=all')
    check('unknown section -> 400', r.status_code == 400, f'HTTP {r.status_code}')
    r = client.get('/catalog/history?type=bogus')
    check('unknown history type -> 400', r.status_code == 400, f'HTTP {r.status_code}')

    # /watching/statuses walks KinoPub's history pages. It used to do that one
    # strictly sequential round trip at a time, up to twenty of them, before
    # the first card could show a watched mark; the walk is now batched, which
    # is exactly the kind of rewrite that hides an off-by-one. `kino_get` is
    # stubbed so these are page-arithmetic checks, not upstream calls.
    import app.main as main_mod

    requested_pages = []

    def history_stub(pages):
        """`pages` maps page number -> (entry count, total_pages) or an Exception."""
        async def fake_kino_get(session, endpoint, params=None):
            page = int((params or {}).get('page') or 1)
            requested_pages.append(page)
            spec = pages.get(page, (0, len(pages)))
            if isinstance(spec, BaseException):
                raise spec
            count, total = spec
            return {'history': [{'item': {'id': 1000 + page * 100 + i, 'watched': 1}} for i in range(count)],
                    'pagination': {'total': total}}
        return fake_kino_get

    real_kino_get = main_mod.kino_get

    def statuses_with(pages):
        del requested_pages[:]
        main_mod.watched_statuses_cache.clear()
        main_mod.kino_get = history_stub(pages)
        try:
            return client.get('/watching/statuses')
        finally:
            main_mod.kino_get = real_kino_get

    r = statuses_with({1: (7, 1)})
    check('a single short history page is one request, not twenty',
          r.status_code == 200 and requested_pages == [1], f'HTTP {r.status_code} pages={requested_pages}')
    check('and every entry on it is counted', r.json()['count'] == 7, r.json()['count'])

    r = statuses_with({1: (50, 3), 2: (50, 3), 3: (12, 3)})
    check('pagination total is honoured exactly - no page 4',
          sorted(requested_pages) == [1, 2, 3], requested_pages)
    check('marks from every page survive', r.json()['count'] == 112, r.json()['count'])

    r = statuses_with({1: (50, 0), 2: (50, 0), 3: (4, 0)})
    check('a missing total falls back to walking until a short page',
          sorted(requested_pages)[:3] == [1, 2, 3] and max(requested_pages) <= 6, requested_pages)
    check('and stops there', r.json()['count'] == 104, r.json()['count'])

    # Pages 2-4 share one batch, so a failure on 2 costs exactly its own 50
    # entries - the siblings alongside it still land (50 + 50 + 5).
    r = statuses_with({1: (50, 4), 2: RuntimeError('upstream hiccup'), 3: (50, 4), 4: (5, 4)})
    check('one failed page loses only its own marks, not the batch',
          r.status_code == 200 and r.json()['count'] == 105, f'HTTP {r.status_code} {r.json().get("count")}')

    # The cached answer must not cost an upstream call, and finishing an
    # episode must not sit behind the TTL.
    main_mod.watched_statuses_cache.clear()
    main_mod.kino_get = history_stub({1: (3, 1)})
    try:
        client.get('/watching/statuses')
        del requested_pages[:]
        client.get('/watching/statuses')
        check('a second call inside the TTL asks upstream for nothing', requested_pages == [], requested_pages)
        client.put('/history', json={'item_id': '1', 'media_id': '1', 'position': 10, 'duration': 100})
        del requested_pages[:]
        client.get('/watching/statuses')
        check('saving progress drops the cached copy', requested_pages == [1], requested_pages)
    finally:
        main_mod.kino_get = real_kino_get

    # Presence: how many clients are using the bridge *now*. The sessions table
    # cannot answer that - it holds a row per authorisation for 45 days - so
    # the count comes from live traffic plus a heartbeat, and these checks pin
    # down that the two never get confused for one another.
    main_mod.presence.clear()
    r = client.get('/presence')
    # `cookies={}` on a request does not clear the client's jar - it merges
    # with it - so the only honest way to ask anonymously is to empty the jar.
    saved_jar = dict(client.cookies)
    client.cookies.clear()
    check('/presence needs a session', client.get('/presence').status_code == 401,
          client.get('/presence').status_code)
    for name, value in saved_jar.items():
        client.cookies.set(name, value)
    body = r.json()
    check('the request that asked is itself a live client', body['clients_online'] == 1, body)
    check('and it is not watching anything', body['watching'] == 0, body)
    check('stored sessions are reported separately from live clients',
          body['sessions_stored'] >= 1 and 'clients_online' in body, body)
    check('the client is identified without exposing the whole session id',
          len(body['clients'][0]['id']) == 8 and body['clients'][0]['id'] != SID,
          body['clients'][0]['id'])

    client.post('/presence/ping', json={'playing': True, 'title': 'Тед Лассо', 'mode': 'hls'})
    body = client.get('/presence').json()
    check('a heartbeat that says "playing" makes the client a viewer',
          body['watching'] == 1 and body['clients'][0]['title'] == 'Тед Лассо', body['clients'])

    client.post('/presence/ping', json={'playing': False})
    body = client.get('/presence').json()
    check('and closing the player drops it immediately, not after the TTL',
          body['watching'] == 0 and body['clients_online'] == 1, body)

    # Relayed playback counts on its own, without any heartbeat: the fragments
    # pass through this process whether the page is responsive or not.
    main_mod.presence.clear()
    main_mod.presence_touch(SID, None, playing=True, mode='relay/hls')
    body = client.get('/presence').json()
    check('a relayed stream alone marks the client as watching', body['watching'] == 1, body)

    # Two devices, one of them stale.
    main_mod.presence.clear()
    main_mod.presence_touch(SID, None)
    main_mod.presence_touch('other-device-sid', None)
    main_mod.presence['other-device-sid']['last_seen'] -= main_mod.PRESENCE_ONLINE_TTL + 5
    body = client.get('/presence').json()
    check('a client that went quiet past the TTL stops being counted',
          body['clients_online'] == 1, body)
    main_mod.presence['other-device-sid']['last_seen'] = time.time()
    body = client.get('/presence').json()
    check('two live devices are two clients', body['clients_online'] == 2, body)
    main_mod.presence.clear()

print(f'\n{len(failures)} FAILURE(S)' if failures else '\nAll checks passed')
sys.exit(1 if failures else 0)
