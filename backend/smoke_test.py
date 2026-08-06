"""Exercises the endpoints that need no upstream call, with a real session row.

Catches what syntax checks and pyflakes cannot: a response that does not match
its own return annotation. FastAPI validates responses against that annotation,
so `-> Dict[str, bool]` on a handler returning a number turns every call into a
500 — which is how a page reload once looked like a lost login.

Run:  docker compose exec -T backend python smoke_test.py
"""
import os
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

    r = client.put('/settings', json={'quality': 'auto', 'subtitle_size': 125})
    check('PUT /settings', r.status_code == 200, f'HTTP {r.status_code} {r.text[:120]}')

    r = client.get('/history')
    check('GET /history (local progress)', r.status_code == 200, f'HTTP {r.status_code} {r.text[:120]}')

    r = client.put('/history', json={'media_id': 'x', 'position': 10, 'duration': 100})
    check('PUT /history', r.status_code == 200, f'HTTP {r.status_code} {r.text[:120]}')

    r = client.get('/debug/events')
    check('GET /debug/events', r.status_code == 200, f'HTTP {r.status_code} {r.text[:120]}')

    r = client.post('/debug/events', json={'message': 'smoke'})
    check('POST /debug/events', r.status_code == 200, f'HTTP {r.status_code} {r.text[:120]}')

    r = client.get('/mock/search?q=')
    check('GET /mock/search', r.status_code == 200, f'HTTP {r.status_code} {r.text[:120]}')

    # Bad input must be rejected cleanly, not blow up.
    r = client.get('/catalog/list?section=nope&feed=all')
    check('unknown section -> 400', r.status_code == 400, f'HTTP {r.status_code}')
    r = client.get('/catalog/history?type=bogus')
    check('unknown history type -> 400', r.status_code == 400, f'HTTP {r.status_code}')

print(f'\n{len(failures)} FAILURE(S)' if failures else '\nAll checks passed')
sys.exit(1 if failures else 0)
