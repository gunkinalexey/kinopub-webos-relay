import asyncio
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import ssl
import time
from collections import OrderedDict
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, Dict, Optional, List
from urllib.parse import quote, urljoin, urlparse, parse_qsl

import httpx
from io import BytesIO
from PIL import Image, ImageOps
from fastapi import Cookie, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel

API_BASE = os.getenv('KINOPUB_API_BASE', 'https://api.service-kp.com').rstrip('/')
CLIENT_ID = os.getenv('KINOPUB_CLIENT_ID', '')
CLIENT_SECRET = os.getenv('KINOPUB_CLIENT_SECRET', '')
COOKIE_SECURE = os.getenv('COOKIE_SECURE', 'false').lower() == 'true'
DB_PATH = Path(os.getenv('DB_PATH', '/data/kp.db'))
STREAM_HOST_SUFFIXES = [x.strip().lower().lstrip('.') for x in os.getenv('STREAM_HOST_SUFFIXES', '').split(',') if x.strip()]
MEDIA_REFERER = os.getenv('MEDIA_REFERER', '')
MEDIA_ORIGIN = os.getenv('MEDIA_ORIGIN', '')
IMAGE_REFERER = os.getenv('IMAGE_REFERER', 'https://kino.watch/')
IMAGE_MAX_BYTES = int(os.getenv('IMAGE_MAX_BYTES', str(8 * 1024 * 1024)))
AUDIO_HLS_ROOT = Path(os.getenv('AUDIO_HLS_ROOT', '/data/audio_hls'))
AUDIO_HLS_TTL = max(600, int(os.getenv('AUDIO_HLS_TTL', str(60 * 60))))
AUDIO_HLS_SEGMENT_SECONDS = max(2, min(10, int(os.getenv('AUDIO_HLS_SEGMENT_SECONDS', '4'))))
AUDIO_HLS_START_BUCKET = max(1, min(30, int(os.getenv('AUDIO_HLS_START_BUCKET', '10'))))


def _ffmpeg_available() -> bool:
    """Whether this build can remux an audio track (`/audio-hls/jobs`).

    Two conditions, both required. `WITH_FFMPEG=0` in .env is the deliberate
    "build without it" switch (see backend/Dockerfile - the image genuinely
    does not contain FFmpeg then, apt is not even contacted). And the binaries
    are looked up regardless, because a flag that promises a program which is
    not installed is worse than no flag: the request would reach
    `create_subprocess_exec` and die with a FileNotFoundError the player has
    no way to explain to the user.

    Nothing else in this bridge depends on FFmpeg - catalogue, playback in all
    three transports, subtitles, and the first three rungs of the audio ladder
    (hls.js renditions, native MP4 tracks, an alternate HLS variant) are all
    untouched by it.
    """
    if os.getenv('WITH_FFMPEG', '1').strip().lower() in {'0', 'false', 'no', 'off', ''}:
        return False
    return bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))


FFMPEG_AVAILABLE = _ffmpeg_available()

pending_devices: Dict[str, Dict[str, Any]] = {}
debug_events = []
page_count_cache: Dict[str, Dict[str, Any]] = {}
profile_cache: Dict[str, Dict[str, Any]] = {}
# Catalogue pages aren't personalised (watched/bookmark state comes from the
# separate /watching/statuses and /history calls, never from /catalog/list
# items) - short TTL is safe and cuts duplicate upstream hits from quick
# back-and-forth sidebar navigation or more than one open tab/session.
CATALOG_LIST_CACHE_TTL = 60
catalog_list_cache: Dict[str, Dict[str, Any]] = {}
PAGE_COUNT_TTL = 6 * 60 * 60
# Genres and countries are reference tables - KinoPub's own values have not
# moved in the lifetime of this project. They were being re-fetched upstream
# every single time the filter panel opened in a fresh tab, which is a real
# round-trip for a list that could be a day old and still correct.
REFERENCE_TTL = 12 * 60 * 60
audio_hls_jobs: Dict[str, Dict[str, Any]] = {}
audio_hls_job_keys: Dict[str, str] = {}


def log_event(kind: str, message: str, details: Optional[Dict[str, Any]] = None) -> None:
    debug_events.append({'at': int(time.time()), 'kind': kind, 'message': message, 'details': details or {}})
    del debug_events[:-200]


def db_connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def db_init() -> None:
    with db_connect() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS sessions (
            sid TEXT PRIMARY KEY,
            access_token TEXT NOT NULL,
            refresh_token TEXT,
            expires_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )''')
        conn.execute('DELETE FROM sessions WHERE updated_at < ?', (time.time() - 60 * 60 * 24 * 45,))
        # Generic expiring key/value store. Exists so caches that are
        # expensive to rebuild survive a container restart - page counts cost
        # a real upstream probe per catalogue section, and the prewarm task
        # re-ran all twelve of them on every `docker compose up -d --build`
        # because the dict holding them lived only in memory.
        conn.execute("CREATE TABLE IF NOT EXISTS kv_cache (key TEXT PRIMARY KEY, payload TEXT NOT NULL, expires_at REAL NOT NULL)")
        conn.execute('DELETE FROM kv_cache WHERE expires_at < ?', (time.time(),))
        conn.execute("CREATE TABLE IF NOT EXISTS user_settings (profile TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL)")
        # watch_progress убрана: она дублировала прогресс, который KinoPub
        # хранит сам (сюда он попадал через v1/watching/marktime, читался из
        # v1/watching). Сверено live - позиции совпадали до секунды и для
        # фильмов, и для серий. Отдельная копия означала лишь два источника
        # правды, расходящиеся при любом сбое зеркалирования.
        conn.execute('DROP TABLE IF EXISTS watch_progress')


def kv_get(key: str) -> Optional[Any]:
    """Read a still-valid entry from the persistent cache, else ``None``."""
    with db_connect() as conn:
        row = conn.execute('SELECT payload, expires_at FROM kv_cache WHERE key = ?', (key,)).fetchone()
    if not row or row['expires_at'] <= time.time():
        return None
    try:
        return json.loads(row['payload'])
    except ValueError:
        return None


def kv_set(key: str, value: Any, ttl: float) -> None:
    with db_connect() as conn:
        conn.execute('INSERT INTO kv_cache(key, payload, expires_at) VALUES (?, ?, ?) '
                     'ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, expires_at=excluded.expires_at',
                     (key, json.dumps(value), time.time() + ttl))


def session_get(sid: Optional[str]) -> Dict[str, Any]:
    if not sid:
        raise HTTPException(401, 'Not authenticated')
    with db_connect() as conn:
        row = conn.execute('SELECT * FROM sessions WHERE sid = ?', (sid,)).fetchone()
    if not row:
        raise HTTPException(401, 'Not authenticated')
    return dict(row)


def session_latest() -> Optional[Dict[str, Any]]:
    with db_connect() as conn:
        row = conn.execute('SELECT * FROM sessions ORDER BY updated_at DESC LIMIT 1').fetchone()
    return dict(row) if row else None


def session_save(sid: str, session: Dict[str, Any]) -> None:
    with db_connect() as conn:
        conn.execute('''INSERT INTO sessions(sid, access_token, refresh_token, expires_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(sid) DO UPDATE SET access_token=excluded.access_token,
            refresh_token=excluded.refresh_token, expires_at=excluded.expires_at,
            updated_at=excluded.updated_at''', (
                sid, session['access_token'], session.get('refresh_token'),
                session['expires_at'], time.time()))


@asynccontextmanager
async def lifespan(app: FastAPI):
    db_init()
    AUDIO_HLS_ROOT.mkdir(parents=True, exist_ok=True)
    # Jobs are process-local. Remove stale fragments left by a previous container.
    for child in AUDIO_HLS_ROOT.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
    app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=15), follow_redirects=False)
    app.state.page_count_task = asyncio.create_task(prewarm_page_counts())
    app.state.audio_hls_cleanup_task = asyncio.create_task(audio_hls_cleanup_loop())
    yield
    for job in list(audio_hls_jobs.values()):
        process = job.get('process')
        if process and process.returncode is None:
            process.terminate()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=2)
            if process.returncode is None:
                process.kill()
    for name in ('page_count_task', 'audio_hls_cleanup_task'):
        task = getattr(app.state, name, None)
        if task and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
    await app.state.http.aclose()


app = FastAPI(title='KinoPub webOS bridge', version='0.9.99', lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in os.getenv('CORS_ORIGINS', 'http://localhost:8080').split(',')],
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)


class DevicePoll(BaseModel):
    code: str


class DebugEvent(BaseModel):
    kind: str = 'frontend'
    message: str
    details: Optional[Dict[str, Any]] = None


class SettingsPayload(BaseModel):
    quality: str = 'auto'
    stream_mode: str = 'auto'
    audio_language: str = 'ru'
    subtitles: str = 'off'
    subtitle_size: int = 100
    autoplay_next: bool = True
    reduce_motion: bool = False
    history_episode_frames: bool = True
    app_icon: str = 'kinopub'
    # off | layer | video. 'video' fullscreens the media element itself, which
    # is what usually promotes it to the hardware video plane (and with it HDR
    # passthrough), at the cost of replacing the custom controls with native ones.
    player_fullscreen: str = 'layer'
    # auto | tv | h264. What to declare to KinoPub as this device's decoding
    # ability (see /device/capabilities). 'auto' trusts the browser's own
    # probes where they answer at all; 'tv' asserts HEVC+4K+HDR outright, for
    # a TV whose browser under-reports itself; 'h264' is the escape hatch for
    # a browser that genuinely cannot decode HEVC and wants a playable list.
    # One device record is shared by every browser on this bridge, so this is
    # also how you stop a desktop visit from re-declaring the TV.
    device_profile: str = 'auto'


class CapabilitiesPayload(BaseModel):
    """What the browser reports it can actually decode/display.

    Drives KinoPub's per-device file selection (see /device/capabilities).

    Tri-state on purpose: ``None`` means "this browser did not answer", which
    is a different thing from ``False`` and must never be written to KinoPub
    as a 0. Defaulting these to ``False`` is what took HEVC (and with it 4K,
    direct playback and HDR) away from the TV - see the endpoint's docstring.
    """
    hevc: Optional[bool] = None
    uhd: Optional[bool] = None
    hdr: Optional[bool] = None


class ProgressPayload(BaseModel):
    media_id: str
    episode_id: Optional[str] = None
    position: float = 0
    duration: float = 0
    completed: bool = False
    # KinoPub's own `v1/watching/marktime`/`toggle` address a video by its
    # ordinal position within a season (starting at 1), not by the internal
    # media id this bridge otherwise uses - the player captures these from
    # the same `collect_media` entry `episode_id` already comes from.
    season: Optional[int] = None
    episode_number: Optional[int] = None


class ServerSelectPayload(BaseModel):
    # Reference id from `v1/references/server-location` (1=Нидерланды,
    # 3=Россия live today), not the two-letter location code.
    id: int


class ServerMeasurePayload(BaseModel):
    # When true the fastest location stays selected once the run finishes;
    # otherwise whatever was selected before the run is put back.
    apply_best: bool = True


class AudioHlsPayload(BaseModel):
    url: str
    # Zero-based position inside media.audios, not KinoPub's audios[].index.
    track: int
    start: float = 0


SENSITIVE_KEYS = {
    'access_token', 'refresh_token', 'token', 'authorization', 'client_secret',
    'password', 'cookie', 'set-cookie', 'secret', 'api_key', 'apikey'
}

def redact_value(value: Any, key: str = '') -> Any:
    key_lower = key.lower().replace('-', '_')
    if any(part in key_lower for part in SENSITIVE_KEYS):
        return '[REDACTED]'
    if isinstance(value, dict):
        return {str(k): redact_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    if isinstance(value, str):
        if value.lower().startswith('bearer '):
            return 'Bearer [REDACTED]'
        if len(value) > 40 and any(x in key_lower for x in ('code', 'session', 'credential')):
            return '[REDACTED]'
    return value

def safe_explorer_path(path: str) -> str:
    path = (path or '').strip().lstrip('/')
    if not path or len(path) > 500:
        raise HTTPException(400, 'Specify a non-empty API path')
    parsed = urlparse('/' + path)
    if parsed.scheme or parsed.netloc or '..' in parsed.path.split('/'):
        raise HTTPException(400, 'Invalid API path')
    if parsed.path.startswith('/oauth2/'):
        raise HTTPException(403, 'OAuth endpoints are not available in API Explorer')
    return parsed.path.lstrip('/')

def require_credentials() -> None:
    if not CLIENT_ID or not CLIENT_SECRET:
        raise HTTPException(503, 'KINOPUB_CLIENT_ID and KINOPUB_CLIENT_SECRET are not configured')


async def refresh_if_needed(sid: str, session: Dict[str, Any]) -> Dict[str, Any]:
    if session['expires_at'] > time.time():
        return session
    refresh_token = session.get('refresh_token')
    if not refresh_token:
        raise HTTPException(401, 'Session expired')
    params = {'grant_type': 'refresh_token', 'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET, 'refresh_token': refresh_token}
    upstream = await app.state.http.post(f'{API_BASE}/oauth2/token', params=params)
    if upstream.status_code >= 400:
        raise HTTPException(401, 'Could not refresh KinoPub token')
    data = upstream.json()
    session.update({'access_token': data['access_token'], 'refresh_token': data.get('refresh_token', refresh_token), 'expires_at': time.time() + int(data.get('expires_in', 3600)) - 60})
    session_save(sid, session)
    log_event('auth', 'Access token refreshed')
    return session



# KinoPub can invalidate an access token well before the `expires_in` it
# handed out - observed live: `/auth/status` still reported ~24h left while
# every real API call came back `{"status":401,"error":"unauthorized"}`.
# `refresh_if_needed` only looks at the clock, so nothing ever refreshed and
# the session stayed dead until the stored deadline finally passed. The fix is
# to treat an upstream 401 itself as the signal, refresh once, and retry.
_refresh_locks: Dict[str, asyncio.Lock] = {}


async def _refresh_now(session: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Refresh this session's token regardless of its stored expiry.

    Returns the refreshed session, or ``None`` when there is nothing to
    refresh with (no sid/refresh token) or KinoPub refused - callers then
    surface the original 401 rather than pretending recovery happened.
    """
    sid = str(session.get('sid') or '')
    if not sid or not session.get('refresh_token'):
        return None
    lock = _refresh_locks.get(sid)
    if lock is None:
        lock = _refresh_locks[sid] = asyncio.Lock()
    async with lock:
        try:
            current = session_get(sid)
        except HTTPException:
            return None
        # A parallel request may have refreshed while this one waited; its
        # new token is already saved, so reuse it instead of burning the
        # rotated refresh token a second time.
        if current.get('access_token') != session.get('access_token'):
            return current
        refresh_token = current.get('refresh_token')
        if not refresh_token:
            return None
        params = {'grant_type': 'refresh_token', 'client_id': CLIENT_ID,
                  'client_secret': CLIENT_SECRET, 'refresh_token': refresh_token}
        try:
            upstream = await app.state.http.post(f'{API_BASE}/oauth2/token', params=params)
        except httpx.RequestError:
            return None
        if upstream.status_code >= 400:
            log_event('auth', 'Token refresh after 401 refused', {'status': upstream.status_code})
            return None
        data = upstream.json()
        current.update({'access_token': data['access_token'],
                        'refresh_token': data.get('refresh_token', refresh_token),
                        'expires_at': time.time() + int(data.get('expires_in', 3600)) - 60})
        session_save(sid, current)
        log_event('auth', 'Access token refreshed after upstream 401')
        return current


def _pick_first(value: Any, keys: List[str], default: Any = None) -> Any:
    if not isinstance(value, dict):
        return default
    for key in keys:
        candidate = value.get(key)
        if candidate not in (None, '', [], {}):
            return candidate
    return default


def _image_url(value: Any, size: str = 'medium') -> str:
    """Extract an image URL from the different structures returned by KinoPub."""
    if isinstance(value, str):
        value = value.strip()
        return value.replace('http://', 'https://', 1) if value.startswith('http://') else value
    if isinstance(value, list):
        # KinoPub can return posters/images as a list of variants. Prefer the
        # requested size, otherwise use the first usable URL found.
        preferred = []
        fallback = []
        for entry in value:
            if isinstance(entry, dict):
                marker = str(entry.get('type') or entry.get('size') or entry.get('name') or '').lower()
                (preferred if size in marker else fallback).append(entry)
            else:
                fallback.append(entry)
        for entry in preferred + fallback:
            found = _image_url(entry, size)
            if found:
                return found
    if isinstance(value, dict):
        keys = ([size, 'big', 'large', 'medium', 'small', 'original', 'url', 'src', 'path', 'image']
                if size != 'big' else
                ['big', 'large', 'original', 'medium', 'small', 'url', 'src', 'path', 'image'])
        for key in keys:
            if key in value:
                found = _image_url(value.get(key), size)
                if found:
                    return found
        # Last-resort recursive walk for undocumented nested wrappers.
        for nested in value.values():
            found = _image_url(nested, size)
            if found:
                return found
    return ''



def _rating_number(value: Any) -> Optional[float]:
    """Return a displayable 0..10 rating, ignoring vote counts and IDs."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, dict):
        for key in ('rating', 'value', 'score', 'rate', 'average', 'avg'):
            if key in value:
                found = _rating_number(value.get(key))
                if found is not None:
                    return found
        return None
    try:
        number = float(str(value).replace(',', '.').strip())
    except (TypeError, ValueError):
        return None
    if number < 0 or number > 10:
        return None
    return round(number, 1)


def _nested_get(data: Any, path: str) -> Any:
    current = data
    for part in path.split('.'):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _extract_rating(raw: Dict[str, Any], paths: List[str]) -> Optional[float]:
    for path in paths:
        value = _nested_get(raw, path)
        rating = _rating_number(value)
        if rating is not None:
            return rating
    return None

def _plain_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(str(value).replace(',', '.').strip())
    except (TypeError, ValueError):
        return None


def _extract_kinopub_rating(raw: Dict[str, Any]) -> tuple[Optional[float], Optional[int]]:
    """Return KinoPub rating on the 0..10 scale.

    KinoPub list payloads commonly expose ``rating`` as the net vote balance
    (positive minus negative) and a separate vote count.  The web UI converts
    those values back to the positive-vote ratio:

        positive = (total + net) / 2
        rating10 = positive / total * 10

    Never expose the raw net balance as the card rating.
    """
    positive_paths = [
        'positive', 'rating_positive', 'votes_positive', 'likes',
        'rating.positive', 'ratings.positive', 'vote.positive', 'votes.positive'
    ]
    negative_paths = [
        'negative', 'rating_negative', 'votes_negative', 'dislikes',
        'rating.negative', 'ratings.negative', 'vote.negative', 'votes.negative'
    ]
    total_paths = [
        'rating_total', 'votes_total', 'total_votes', 'votes', 'vote_count',
        'votes_count', 'rating_votes', 'rating_votes_count', 'total_rating_votes',
        'rating.total', 'ratings.total', 'vote.total', 'votes.total',
        'rating.votes', 'ratings.votes'
    ]
    positive = next((_plain_number(_nested_get(raw, x)) for x in positive_paths if _plain_number(_nested_get(raw, x)) is not None), None)
    negative = next((_plain_number(_nested_get(raw, x)) for x in negative_paths if _plain_number(_nested_get(raw, x)) is not None), None)
    total = next((_plain_number(_nested_get(raw, x)) for x in total_paths if _plain_number(_nested_get(raw, x)) is not None), None)

    if total is None and positive is not None and negative is not None:
        total = positive + negative
    if positive is not None and total and total > 0:
        return round(max(0.0, min(10.0, positive / total * 10.0)), 1), None

    net = _plain_number(raw.get('rating'))
    if net is not None and total and total > 0 and abs(net) <= total:
        positive_from_net = (total + net) / 2.0
        normalized = positive_from_net / total * 10.0
        return round(max(0.0, min(10.0, normalized)), 1), None

    normalized = _extract_rating(raw, [
        'user_rating', 'rating_user', 'kinopub_rating', 'rating_kinopub',
        'rating_value', 'rating_average', 'rating_avg', 'rating_ratio',
        'ratings.kinopub', 'ratings.user', 'rating.kinopub', 'rating.user',
        'rating.value', 'rating.average', 'rating.avg'
    ])
    if normalized is not None:
        return normalized, None
    return None, None


def _extract_watched_status(raw: Dict[str, Any]) -> int:
    """Return -1/0/1 for not watched / started / watched."""
    candidates = [
        raw.get('watched'),
        _nested_get(raw, 'watching.status'),
        _nested_get(raw, 'watching.watched'),
        _nested_get(raw, 'status.watched'),
    ]
    for value in candidates:
        if isinstance(value, bool):
            return 1 if value else -1
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number in (-1, 0, 1):
            return number
    return -1


def normalize_catalog_item(raw: Dict[str, Any]) -> Dict[str, Any]:
    title = str(_pick_first(raw, ['title', 'name'], 'Без названия'))
    original = str(_pick_first(raw, ['original_title', 'original', 'title_original'], ''))
    if ' / ' in title and not original:
        title, original = [part.strip() for part in title.split(' / ', 1)]
    genres_raw = raw.get('genres') or []
    genres = []
    if isinstance(genres_raw, list):
        for value in genres_raw:
            if isinstance(value, dict):
                genres.append(str(value.get('title') or value.get('name') or ''))
            elif value:
                genres.append(str(value))
    item_id = str(raw.get('id', '')).strip()
    poster = _image_url(raw.get('posters') or raw.get('poster') or raw.get('images') or raw.get('image'))
    # The public KinoPub frontend uses a stable poster path derived from item ID.
    # This also covers compact API payloads where no posters field is included.
    # 'big' is 500x750; 'medium' is only 250x375 and was being upscaled.
    if not poster and item_id.isdigit():
        poster = f'https://m.staticpop.net/poster/item/big/{item_id}.jpg'
    # The CDN also serves a real 16:9 backdrop under 'wide' (up to 3840x2160).
    # Falling back to the poster meant stretching a 2:3 image across a wide
    # strip, which is why it looked soft and badly cropped. 'wide' is missing
    # for a fair share of items, so the poster stays as the fallback and the
    # image proxy switches to it when the wide URL 404s.
    backdrop = _image_url(raw.get('background') or raw.get('backdrop') or raw.get('covers'), 'big')
    if not backdrop and item_id.isdigit():
        backdrop = f'https://m.staticpop.net/poster/item/wide/{item_id}.jpg'
    backdrop = backdrop or _image_url(raw.get('posters'), 'big') or poster
    # KinoPub payloads vary between list/detail endpoints. Never derive one
    # rating from another: each badge is populated only from its own source.
    kinopub_rating, kinopub_score = _extract_kinopub_rating(raw)
    imdb_rating = _extract_rating(raw, [
        'imdb_rating', 'rating_imdb', 'imdb', 'ratings.imdb',
        'rating.imdb', 'external_ratings.imdb'
    ])
    kinopoisk_rating = _extract_rating(raw, [
        'kinopoisk_rating', 'rating_kinopoisk', 'rating_kp', 'kp_rating',
        'kinopoisk', 'ratings.kinopoisk', 'ratings.kp',
        'rating.kinopoisk', 'rating.kp', 'external_ratings.kinopoisk'
    ])
    return {
        'id': item_id,
        'type': str(raw.get('type') or 'movie'),
        'subtype': raw.get('subtype'),
        'title': title,
        'original_title': original,
        'year': raw.get('year'),
        'rating': kinopub_rating,
        'kinopub_score': kinopub_score,
        'imdb_rating': imdb_rating,
        'kinopoisk_rating': kinopoisk_rating,
        'genres': [x for x in genres if x],
        'poster': poster,
        'backdrop': backdrop,
        'backdrop_fallback': poster if backdrop != poster else '',
        'description': str(_pick_first(raw, ['plot', 'description', 'overview'], '')),
        'watched': _extract_watched_status(raw),
        # Catalogue list payloads (unlike a single item's detail/media node)
        # never carry subtitle info, so the grid card can only be honest about
        # Dolby (real per-item `ac3` flag) and resolution (real `quality`) -
        # not subtitles, which would otherwise be a fake always-on icon.
        'has_dolby': bool(raw.get('ac3')),
        'quality': raw.get('quality') if isinstance(raw.get('quality'), (int, float)) else None,
        # Only present on a single item's own detail fetch (`v1/items/{id}`),
        # not on list payloads - real "Буду смотреть" tracking flag, the same
        # one `v1/watching/serials?subscribed=1` filters on.
        'subscribed': bool(raw.get('subscribed')),
    }


def extract_catalog_items(payload: Any) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    def walk(value: Any, key: str = '') -> None:
        if isinstance(value, list):
            if value and all(isinstance(x, dict) for x in value):
                likely = [x for x in value if 'id' in x and ('title' in x or 'name' in x) and ('type' in x or 'posters' in x or 'year' in x)]
                candidates.extend(likely)
            for item in value:
                walk(item, key)
        elif isinstance(value, dict):
            for child_key, child in value.items():
                walk(child, str(child_key))
    walk(payload)
    seen = set()
    result = []
    for raw in candidates:
        item = normalize_catalog_item(raw)
        if item['id'] and item['id'] not in seen:
            seen.add(item['id'])
            result.append(item)
    return result


def stream_from_file(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    urls = raw.get('urls') or raw.get('url') or {}
    if isinstance(urls, str):
        urls = {'http': urls}
    if not isinstance(urls, dict):
        return []
    result = []
    for protocol in ('hls', 'hls2', 'hls4', 'http'):
        url = urls.get(protocol)
        if isinstance(url, str) and url.startswith(('http://', 'https://')):
            result.append({
                'url': url,
                'protocol': 'hls' if protocol.startswith('hls') else 'http',
                'source_type': protocol,
                'quality': raw.get('quality') or (str(raw.get('h')) + 'p' if raw.get('h') else ''),
                'height': raw.get('h'),
                'width': raw.get('w'),
                'codec': raw.get('codec'),
                'hevc': is_hevc(raw.get('codec')),
                # KinoPub does not always flag HDR explicitly, so fall back to
                # the quality label. Used as a hint only, never to gate playback.
                'hdr': bool(raw.get('hdr')) or 'hdr' in str(raw.get('quality') or '').lower(),
                'bitrate': raw.get('bitrate') or raw.get('br'),
                'file': raw.get('file'),
            })
    return result


def merge_unique_list(target: List[Any], incoming: Any, key_builder) -> List[Any]:
    """Append entries from ``incoming`` that ``target`` does not already contain."""
    if not isinstance(incoming, list):
        return target
    known = {key_builder(value) for value in target}
    for value in incoming:
        marker = key_builder(value)
        if marker not in known:
            target.append(value)
            known.add(marker)
    return target


def stream_key(value: Any) -> str:
    if not isinstance(value, dict):
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return '|'.join(str(value.get(name) or '') for name in ('url', 'file', 'source_type', 'quality', 'height', 'codec'))


def audio_key(value: Any) -> str:
    if not isinstance(value, dict):
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    # ID is the most stable key; index is the position KinoPub assigns to the
    # track inside the file. Include both because either one can be absent.
    return '|'.join(str(value.get(name) or '') for name in ('id', 'index', 'track_index', 'stream_index', 'lang'))


def subtitle_key(value: Any) -> str:
    if not isinstance(value, dict):
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return '|'.join(str(value.get(name) or '') for name in ('url', 'file', 'lang', 'shift', 'embed'))


def track_numbers(value: Any) -> List[str]:
    if isinstance(value, list):
        raw_values: List[Any] = list(value)
    elif value is None:
        raw_values = []
    else:
        raw_values = re.split(r'[^0-9]+', str(value))
    result: List[str] = []
    for raw_value in raw_values:
        try:
            number = int(raw_value)
        except (TypeError, ValueError):
            continue
        if number > 0 and str(number) not in result:
            result.append(str(number))
    return result


def expected_track_count(value: Any) -> int:
    """Number of audio tracks KinoPub claims the media has.

    ``tracks`` is a plain count in most payloads and a list of track numbers in
    a few. Either way it is only a hint used to detect a truncated ``audios``
    list, never to hide entries the API did return.
    """
    if isinstance(value, list):
        return len(value)
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return max(0, int(str(value).strip()))
    except (TypeError, ValueError):
        return len(track_numbers(value))


def sorted_audios(audios: Any) -> List[Any]:
    """Order audio entries the way FFmpeg sees them inside the file.

    The player selects a track by its *position* in this list (``0:a:N``), so
    the order must match the file. KinoPub numbers tracks in ``index``; entries
    without one keep their original relative position at the end.
    """
    if not isinstance(audios, list):
        return []
    decorated = []
    for position, entry in enumerate(audios):
        raw = entry.get('index') if isinstance(entry, dict) else None
        try:
            decorated.append(((0, int(raw), position), entry))
        except (TypeError, ValueError):
            decorated.append(((1, 0, position), entry))
    decorated.sort(key=lambda pair: pair[0])
    return [entry for _, entry in decorated]


def collect_media(payload: Any) -> List[Dict[str, Any]]:
    media: List[Dict[str, Any]] = []
    def walk(value: Any, context: Dict[str, Any], key: str = '') -> None:
        if isinstance(value, list):
            for index, child in enumerate(value):
                next_context = dict(context)
                if key == 'seasons' and isinstance(child, dict):
                    next_context['season'] = child.get('number') or child.get('id') or index + 1
                elif key in {'episodes', 'videos', 'media'} and isinstance(child, dict):
                    next_context['episode'] = child.get('number') or child.get('video') or index + 1
                walk(child, next_context, key)
            return
        if not isinstance(value, dict):
            return
        files = value.get('files')
        streams: List[Dict[str, Any]] = []
        if isinstance(files, list):
            for file_value in files:
                if isinstance(file_value, dict):
                    streams.extend(stream_from_file(file_value))
        is_media = key in {'episodes', 'videos', 'media'} or bool(streams) or ('duration' in value and (value.get('id') is not None or value.get('media_id') is not None) and key != 'item')
        media_identifier = value.get('media_id') if value.get('media_id') is not None else value.get('id')
        if is_media and (media_identifier is not None or streams):
            media.append({
                'id': str(media_identifier if media_identifier is not None else 'direct-' + str(len(media) + 1)),
                'title': str(value.get('title') or value.get('name') or ('Серия ' + str(context.get('episode') or len(media) + 1))),
                'season': context.get('season'),
                'episode': value.get('number') or context.get('episode'),
                'duration': value.get('duration'),
                'thumbnail': _image_url(value.get('thumbnail') or value.get('posters')),
                'streams': streams,
                'audios': value.get('audios') or [],
                'tracks': value.get('tracks'),
                'subtitles': value.get('subtitles') or [],
            })
        for child_key, child in value.items():
            if child_key != 'files':
                walk(child, dict(context), str(child_key))
    walk(payload, {})
    # The same media node can occur more than once in a KinoPub payload.
    # Older code kept the first occurrence, which could contain only one audio
    # entry while a later duplicate had the complete metadata. Merge duplicates
    # instead of discarding them.
    by_id: Dict[str, Dict[str, Any]] = {}
    out: List[Dict[str, Any]] = []

    for item in media:
        key = item['id']
        existing = by_id.get(key)
        if existing is None:
            existing = item
            existing['streams'] = list(existing.get('streams') or [])
            existing['audios'] = list(existing.get('audios') or [])
            existing['subtitles'] = list(existing.get('subtitles') or [])
            by_id[key] = existing
            out.append(existing)
            continue

        merge_unique_list(existing['streams'], list(item.get('streams') or []), stream_key)
        merge_unique_list(existing['audios'], list(item.get('audios') or []), audio_key)
        merge_unique_list(existing['subtitles'], list(item.get('subtitles') or []), subtitle_key)

        merged_tracks = track_numbers(existing.get('tracks'))
        for number in track_numbers(item.get('tracks')):
            if number not in merged_tracks:
                merged_tracks.append(number)
        if merged_tracks:
            existing['tracks'] = ','.join(merged_tracks)

        for field in ('title', 'season', 'episode', 'duration', 'thumbnail'):
            if not existing.get(field) and item.get(field):
                existing[field] = item[field]

    for entry in out:
        entry['audios'] = sorted_audios(entry.get('audios'))
    return out


HEVC_CODECS = {'hevc', 'h265', 'hvc1', 'hev1', 'x265'}


def is_hevc(codec: Any) -> bool:
    return str(codec or '').lower() in HEVC_CODECS


def choose_best_stream(streams: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Rank candidates by picture quality, best first.

    The previous ordering aimed at 1080p H.264 and treated everything else as a
    defect: HEVC was penalised outright and ``abs(height - 1080)`` made 2160p
    score exactly as badly as 0p, so 4K was never selected and 720p H.264 even
    outranked 1080p HEVC. Since HEVC is what carries the 10-bit and HDR
    variants, that also discarded the widest colour on offer.

    Resolution leads, then HEVC over H.264 at equal size, then the plain HTTP
    file so the client can hand it to a hardware decoder. The client still has
    the final say: only it knows which codecs it can actually play.
    """
    if not streams:
        return None
    def score(stream: Dict[str, Any]) -> tuple:
        height = int(stream.get('height') or 0)
        codec = str(stream.get('codec') or '').lower()
        codec_rank = 0 if is_hevc(codec) else (1 if codec in {'h264', 'avc', 'avc1', ''} else 2)
        protocol_rank = {'http': 0, 'hls': 1, 'hls2': 2, 'hls4': 3}.get(str(stream.get('source_type')), 4)
        return (-height, codec_rank, protocol_rank)
    return sorted(streams, key=score)[0]


async def kino_get(session: Dict[str, Any], path: str, params: Optional[Dict[str, Any]] = None, retry_auth: bool = True) -> Any:
    query = dict(params or {})
    query['access_token'] = session['access_token']
    try:
        response = await app.state.http.get(f"{API_BASE}/{path.lstrip('/')}", params=query, headers={'Accept': 'application/json'})
    except httpx.TimeoutException as exc:
        raise HTTPException(504, 'KinoPub API timeout') from exc
    except httpx.RequestError as exc:
        raise HTTPException(502, f'Could not connect to KinoPub API: {exc}') from exc
    if response.status_code == 401 and retry_auth:
        refreshed = await _refresh_now(session)
        if refreshed:
            session.update(refreshed)
            return await kino_get(session, path, params, retry_auth=False)
    if response.status_code >= 400:
        raise HTTPException(response.status_code, response.text[:1000])
    try:
        return response.json()
    except ValueError as exc:
        raise HTTPException(502, 'KinoPub API returned invalid JSON') from exc


async def kino_post(session: Dict[str, Any], path: str, body: Optional[Dict[str, Any]] = None, retry_auth: bool = True) -> Any:
    query = {'access_token': session['access_token']}
    try:
        response = await app.state.http.post(f"{API_BASE}/{path.lstrip('/')}", params=query, json=body or {}, headers={'Accept': 'application/json'})
    except httpx.TimeoutException as exc:
        raise HTTPException(504, 'KinoPub API timeout') from exc
    except httpx.RequestError as exc:
        raise HTTPException(502, f'Could not connect to KinoPub API: {exc}') from exc
    if response.status_code == 401 and retry_auth:
        refreshed = await _refresh_now(session)
        if refreshed:
            session.update(refreshed)
            return await kino_post(session, path, body, retry_auth=False)
    if response.status_code >= 400:
        raise HTTPException(response.status_code, response.text[:1000])
    try:
        return response.json()
    except ValueError as exc:
        raise HTTPException(502, 'KinoPub API returned invalid JSON') from exc


# A sidebar section maps to either a KinoPub content type or a genre filter.
# "Аниме" is genre 25, not a type: /v1/items?type=anime returns nothing, which
# is why that section came up empty. The site's /anime page is the same genre
# filter, so it spans both films and series. 3D is a real type of its own.
CATALOG_SECTIONS: Dict[str, Dict[str, Any]] = {
    'movie': {'type': 'movie'},
    'serial': {'type': 'serial'},
    '3d': {'type': '3d'},
    'anime': {'genre': 25},
    'concert': {'type': 'concert'},
    'documovie': {'type': 'documovie'},
    'docuserial': {'type': 'docuserial'},
    'tvshow': {'type': 'tvshow'},
    'sport': {'type': 'sport'},
}


def section_params(section: str) -> Dict[str, Any]:
    """Query parameters that select one sidebar section upstream."""
    return dict(CATALOG_SECTIONS.get(section) or {})
CATALOG_FEEDS = {
    'popular': 'v1/items/popular',
    'fresh': 'v1/items/fresh',
    'hot': 'v1/items/hot',
    'all': 'v1/items',
}

@app.get('/catalog/list')
async def catalog_list(
    section: str = 'movie', feed: str = 'fresh', page: int = 0, perpage: int = 48,
    genre: Optional[int] = None, country: Optional[int] = None,
    year_from: Optional[int] = None, year_to: Optional[int] = None, added_days: Optional[int] = None,
    imdb_from: Optional[float] = None, imdb_to: Optional[float] = None,
    kp_from: Optional[float] = None, kp_to: Optional[float] = None,
    quality: Optional[int] = None, sort: Optional[str] = None,
    kp_session: Optional[str] = Cookie(default=None),
) -> Dict[str, Any]:
    """Return one explicitly typed KinoPub catalogue section.

    Each sidebar section is sent to KinoPub with its own API ``type`` value,
    rather than being approximated by filtering a mixed movie/serial payload.

    The optional filters mirror real, verified `v1/items` parameters - not
    guessed. Three things are non-obvious and were checked live before
    shipping:

    - `quality` is the small **reference id** from
      `v1/references/video-quality` (1=480p..4=4K), not the raw resolution -
      `quality=2160` silently returns zero results, `quality=4` returns the
      same 2160p titles.
    - `sort` takes a bare field name
      (id/year/title/created/updated/rating/views/watchers), `-field` for
      descending.
    - Year/date-added ranges go through `conditions[]=<field><op><value>`
      (e.g. `conditions[]=year>=1990`), documented only as a one-line example
      ("year <= 100") with no query-string encoding shown. Confirmed live
      that repeated `conditions[]` params really do AND together and really
      do filter (`year<=1950` dropped a ~7900-item type=serial list to ~12).
      Also confirmed live that the documented `finished` (0/1, "статус
      сериала") param does **nothing** at all - identical item count with
      and without it - so it is deliberately not exposed here; the filter
      panel has no "Статус" control for the same reason.
    - **Rating ranges do exist**, as `conditions[]` fields named
      `imdb_rating` and `kinopoisk_rating`. An earlier version of this
      docstring claimed they did not - that was wrong, and it cost the
      filter panel two controls. Confirmed live, three ways: `imdb_rating>=8`
      cuts type=movie from ~31 990 to ~795 and every returned title really
      does score >= 8; `imdb_rating>=7` + `imdb_rating<=8` AND together into
      a genuine band; and an invented field (`bogus_field>=8`) leaves the
      count *untouched*, which is what proves the endpoint recognises the
      real two rather than ignoring all three alike.
      Two honest caveats, both from live output. **The decimal part of the
      bound is discarded upstream**: `imdb_rating>=7`, `>=7.1`, `>=7.5` and
      `>=7.9` all return the identical 8444 pages, and the same holds for
      `<=` and for `kinopoisk_rating` - so these bounds are whole numbers in
      practice and the panel's sliders step by 1 rather than displaying a
      precision the API will not honour. Values are floored here, which
      reproduces upstream exactly (rounding 8.6 up to 9 would filter out
      titles KinoPub itself would have returned). And a `<=` bound also
      matches titles with no rating recorded at all, so an upper bound reads
      "at most X, or unrated". Only bounds the user actually moved are sent.
    - Age rating/language/translation/voice studio/subtitles/AC3, all
      visible on kino.watch's own filter panel, were each tried live as
      `v1/items` params (`age`, `lang`, `language`, `translation`, `voice`,
      `subtitles`, `subtitle`, `ac3`, `advert`, `hd`, `nohd`, `uhd`) and
      every one returned the unfiltered count. They do not exist here, so
      faking a control for them was rejected rather than shipped
      non-functional.
    """
    section = section.strip().lower()
    feed = feed.strip().lower()
    selector = section_params(section)
    endpoint = CATALOG_FEEDS.get(feed)
    if not selector:
        raise HTTPException(400, f'Unknown catalogue section: {section}')
    # "Аниме" *is* a genre selector under the hood (genre=25, see
    # CATALOG_SECTIONS) rather than a real `type`. `v1/items` only accepts
    # one `genre` value, so blindly overwriting it with the filter panel's
    # own genre choice silently dropped the anime constraint entirely -
    # confirmed live: filtering Anime by "Комедия" returned 13319 ordinary
    # comedies (Незнайка на Луне etc.), not the ~1730 titles actually
    # tagged Аниме. There's no documented way to AND two genres together on
    # this endpoint, so the section's own genre wins; the filter panel also
    # hides its Genre dropdown on this section so this is defense in depth,
    # not the only guard.
    if genre is not None and 'genre' not in selector:
        selector['genre'] = genre
    if country is not None:
        selector['country'] = country
    if quality is not None:
        selector['quality'] = quality
    if sort:
        selector['sort'] = sort
    conditions: List[str] = []
    if year_from is not None:
        conditions.append(f'year>={year_from}')
    if year_to is not None:
        conditions.append(f'year<={year_to}')
    if added_days is not None and added_days > 0:
        conditions.append(f'created>={int(time.time()) - added_days * 86400}')
    # Floored, not rounded: KinoPub throws the decimal away itself (verified,
    # see above), so flooring is what actually happened either way, while
    # rounding up would quietly exclude titles the raw API would have kept.
    for value, field, op in ((imdb_from, 'imdb_rating', '>='), (imdb_to, 'imdb_rating', '<='),
                             (kp_from, 'kinopoisk_rating', '>='), (kp_to, 'kinopoisk_rating', '<=')):
        if value is not None:
            conditions.append(f'{field}{op}{int(max(0.0, min(10.0, float(value))))}')
    if conditions:
        selector['conditions[]'] = conditions
    if not endpoint:
        raise HTTPException(400, f'Unknown catalogue feed: {feed}')
    # The UI uses zero-based indexes internally, while KinoPub's catalogue
    # treats page=1 as the first page. page=0 is accepted upstream but aliases
    # page=1, which previously made UI pages 1 and 2 show identical results.
    page = max(0, min(page, 9999))
    api_page = page + 1
    perpage = max(1, min(perpage, 100))
    cache_key = json.dumps({'endpoint': endpoint, 'selector': selector, 'page': api_page, 'perpage': perpage}, sort_keys=True)
    cached = catalog_list_cache.get(cache_key)
    if cached and time.time() - cached['at'] < CATALOG_LIST_CACHE_TTL:
        return cached['data']
    session = await refresh_if_needed(kp_session or '', session_get(kp_session))
    payload = await kino_get(session, endpoint, {**selector, 'page': api_page, 'perpage': perpage})
    items = extract_catalog_items(payload)
    for item in items:
        item['section'] = section
    log_event('catalog', 'Catalogue section loaded', {
        'section': section, 'selector': selector, 'feed': feed, 'page': page, 'api_page': api_page, 'count': len(items)
    })
    totals = _pagination_values(payload, perpage)
    total_pages, total_items = totals['total_pages'], totals['total_items']
    # A full page is evidence that a following page may exist. Some KinoPub
    # shortcut responses omit totals or expose stale/ambiguous pagination data.
    has_next = (page + 1 < total_pages) if total_pages > 1 else (len(items) >= perpage)
    result = {
        'section': section,
        'selector': selector,
        'feed': feed,
        'page': page,
        'perpage': perpage,
        'total_items': total_items,
        'total_pages': total_pages,
        'has_next': has_next,
        'items': items,
    }
    catalog_list_cache[cache_key] = {'at': time.time(), 'data': result}
    return result



def _page_count_cache_key(section: str, feed: str, perpage: int) -> str:
    return f"{section}:{feed}:{perpage}"


def _first_int(*values: Any) -> int:
    for value in values:
        try:
            if value is not None and str(value) != '':
                return int(value)
        except (TypeError, ValueError):
            pass
    return 0


def _pagination_values(payload: Any, perpage: int) -> Dict[str, int]:
    """Page and item totals from a KinoPub list response.

    KinoPub uses ``pagination.total`` for the number of pages; the item count,
    when present, is exposed separately as ``total_count`` / ``total_items``.
    """
    pagination = payload.get('pagination') if isinstance(payload, dict) and isinstance(payload.get('pagination'), dict) else {}
    if not pagination and isinstance(payload, dict) and isinstance(payload.get('meta'), dict):
        pagination = payload.get('meta')

    total_pages = _first_int(
        pagination.get('total'), pagination.get('pages'), pagination.get('total_pages'),
        pagination.get('page_count'), pagination.get('last_page'),
        payload.get('pages') if isinstance(payload, dict) else None,
        payload.get('total_pages') if isinstance(payload, dict) else None,
    )
    total_items = _first_int(
        pagination.get('total_count'), pagination.get('total_items'), pagination.get('items_count'),
        payload.get('total_count') if isinstance(payload, dict) else None,
        payload.get('total_items') if isinstance(payload, dict) else None,
    )
    if not total_items and total_pages:
        # Upper-bound estimate until the final page is loaded.
        total_items = total_pages * perpage
    return {'total_pages': max(0, total_pages), 'total_items': max(0, total_items)}


async def _catalog_page_probe(session: Dict[str, Any], endpoint: str, selector: Dict[str, Any], page: int, perpage: int) -> Dict[str, Any]:
    payload = await kino_get(session, endpoint, {**selector, 'page': page, 'perpage': perpage})
    items = extract_catalog_items(payload)
    signature = tuple(str(item.get('id', '')) for item in items if item.get('id') is not None)
    page_meta = _pagination_values(payload, perpage)
    return {
        'count': len(items),
        'signature': signature,
        'total_pages': page_meta['total_pages'],
        'total_items': page_meta['total_items'],
    }


async def _discover_page_count(session: Dict[str, Any], section: str, feed: str, perpage: int, refresh: bool = False) -> Dict[str, Any]:
    """Find the last distinct upstream page.

    KinoPub may repeat the final page for out-of-range page numbers instead of
    returning an empty list. A page is therefore considered invalid when it is
    empty or repeats either page 1 or the immediately preceding page.
    """
    selector = section_params(section)
    endpoint = CATALOG_FEEDS[feed]
    key = _page_count_cache_key(section, feed, perpage)
    cached = page_count_cache.get(key)
    if cached and not refresh and cached.get('expires_at', 0) > time.time():
        return {k: v for k, v in cached.items() if k != 'expires_at'}
    # Second chance before probing upstream: a previous container already
    # worked this out and it is still inside the 6h window. Without this the
    # prewarm task re-probed all twelve sections on every restart.
    if not refresh:
        stored = kv_get('page_count:' + key)
        if stored:
            page_count_cache[key] = {**stored, 'expires_at': time.time() + PAGE_COUNT_TTL}
            return stored

    probe_cache: Dict[int, Dict[str, Any]] = {}
    probes = 0

    async def probe(page: int) -> Dict[str, Any]:
        nonlocal probes
        page = max(1, page)
        if page not in probe_cache:
            probe_cache[page] = await _catalog_page_probe(session, endpoint, selector, page, perpage)
            probes += 1
        return probe_cache[page]

    first = await probe(1)
    if first.get('total_pages', 0) > 0:
        result = {
            'section': section,
            'feed': feed,
            'perpage': perpage,
            'total_pages': first['total_pages'],
            'total_items': first.get('total_items', 0),
            'exact': True,
            'source': 'api-pagination',
            'probes': probes,
        }
    elif first['count'] == 0:
        result = {'section': section, 'feed': feed, 'perpage': perpage, 'total_pages': 0, 'total_items': 0, 'exact': True, 'probes': probes}
    elif first['count'] < perpage:
        result = {'section': section, 'feed': feed, 'perpage': perpage, 'total_pages': 1, 'total_items': first['count'], 'exact': True, 'probes': probes}
    else:
        first_signature = first['signature']

        async def is_valid(page: int) -> bool:
            current = await probe(page)
            if current['count'] == 0:
                return False
            if page <= 1:
                return True
            previous = await probe(page - 1)
            if current['signature'] and current['signature'] == previous['signature']:
                return False
            if current['signature'] and current['signature'] == first_signature:
                return False
            return True

        low, high = 1, 2
        max_page = 10000
        boundary_found = False
        while high <= max_page:
            if not await is_valid(high):
                boundary_found = True
                break
            low = high
            current = await probe(high)
            if current['count'] < perpage:
                high += 1
                boundary_found = True
                break
            high *= 2

        if high > max_page:
            high = max_page + 1

        while low + 1 < high:
            mid = (low + high) // 2
            if await is_valid(mid):
                low = mid
            else:
                high = mid
                boundary_found = True

        total_pages = low
        last = await probe(total_pages)
        total_items = max(0, (total_pages - 1) * perpage + last['count'])
        result = {
            'section': section, 'feed': feed, 'perpage': perpage,
            'total_pages': total_pages, 'total_items': total_items,
            'exact': boundary_found, 'probes': probes,
        }

    page_count_cache[key] = {**result, 'expires_at': time.time() + PAGE_COUNT_TTL}
    kv_set('page_count:' + key, result, PAGE_COUNT_TTL)
    log_event('catalog', 'Catalogue page count discovered', result)
    return result


async def prewarm_page_counts(force: bool = False, sid: Optional[str] = None) -> None:
    """Precompute catalogue sizes in the background using the latest session."""
    await asyncio.sleep(1)
    try:
        row = session_get(sid) if sid else session_latest()
        if not row:
            log_event('catalog', 'Page-count prewarm skipped: no saved session')
            return
        session_sid = sid or str(row.get('sid', ''))
        session = await refresh_if_needed(session_sid, row)
        targets = [
            ('movie', 'popular'), ('movie', 'fresh'), ('movie', 'hot'),
            ('movie', 'all'), ('serial', 'all'), ('3d', 'all'), ('anime', 'all'), ('concert', 'all'),
            ('documovie', 'all'), ('docuserial', 'all'),
            ('tvshow', 'all'), ('sport', 'all'),
        ]
        for section, feed in targets:
            try:
                await _discover_page_count(session, section, feed, 48, refresh=force)
            except Exception as exc:
                log_event('catalog', 'Page-count prewarm target failed', {
                    'section': section, 'feed': feed, 'error': str(exc)
                })
        log_event('catalog', 'Page-count prewarm completed', {'targets': len(targets)})
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log_event('catalog', 'Page-count prewarm failed', {'error': str(exc)})


@app.get('/catalog/page-count')
async def catalog_page_count(section: str = 'movie', feed: str = 'fresh', perpage: int = 48, refresh: bool = False, kp_session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    section = section.strip().lower()
    feed = feed.strip().lower()
    if section not in CATALOG_SECTIONS:
        raise HTTPException(400, f'Unknown catalogue section: {section}')
    if feed not in CATALOG_FEEDS:
        raise HTTPException(400, f'Unknown catalogue feed: {feed}')
    perpage = max(1, min(perpage, 100))
    session = await refresh_if_needed(kp_session or '', session_get(kp_session))
    return await _discover_page_count(session, section, feed, perpage, refresh=refresh)


HISTORY_TYPES = ['movie', 'serial', '3d', 'concert', 'documovie', 'docuserial', 'tvshow']
# Two aggregate tabs on top of the per-type ones - kino.watch's own history
# page has both "Все фильмы"/"Все эпизоды" (aggregates) and the individual
# type tabs side by side, not one or the other. `v1/history` has no concept
# of these itself (no `type` param at all, aggregate or otherwise - see the
# note below); they are purely this bridge grouping the same real `type`
# field into "standalone" vs "episodic" content, using the exact same split
# the details screen's duration display already relies on (`serial`/
# `docuserial`/`tvshow` are the types with real seasons/episodes; everything
# else - `movie`/`3d`/`concert`/`documovie` - is one watch, one entry).
HISTORY_GROUPS = {
    'movies': {'movie', '3d', 'concert', 'documovie'},
    'episodes': {'serial', 'docuserial', 'tvshow'},
}
# v1/history ignores a `type` parameter: it returns the same page whatever is
# passed. Filtering therefore has to happen here, which means one upstream page
# no longer fills one UI page, so several are scanned and sliced locally.
HISTORY_SCAN_PAGES = 20
HISTORY_SCAN_PERPAGE = 50
HISTORY_CACHE_TTL = 180
history_cache: Dict[str, Dict[str, Any]] = {}


def _history_entry_item(entry: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(entry, dict):
        return None
    raw = entry.get('item') if isinstance(entry.get('item'), dict) else {}
    if not raw:
        return None
    item = normalize_catalog_item(raw)
    if not item['id']:
        return None
    media = entry.get('media') if isinstance(entry.get('media'), dict) else {}
    # `last_seen`/`first_seen` are the real Unix timestamps of when this was
    # watched. `time` and `counter` are also present on every entry (seconds
    # actually watched, and a view counter) and used to get picked first here,
    # which read e.g. "373 seconds watched" as "watched at 00:06:13 on 1 Jan
    # 1970" - every entry landed in the same bogus day bucket regardless of
    # when it was actually watched, which looked like history had frozen.
    watched = _plain_number(_pick_first(entry, ['last_seen', 'first_seen']))
    item['watched_at'] = int(watched) if watched else 0
    item['media_title'] = str(media.get('title') or '') if media else ''
    item['director'] = ', '.join(_name_list(raw.get('director') or raw.get('directors')))
    # `snumber`/`number` identify which season/episode this entry is for a
    # series (0/absent for a movie); `thumbnail` is a real frame grabbed from
    # that specific episode's file, not the show's generic poster.
    season = media.get('snumber') if media else None
    episode = media.get('number') if media else None
    item['history_season'] = int(season) if isinstance(season, (int, float)) and season else 0
    item['history_episode'] = int(episode) if isinstance(episode, (int, float)) and episode else 0
    item['episode_thumbnail'] = str(media.get('thumbnail') or '') if media else ''
    return item


async def _history_fetch(session: Dict[str, Any], api_page: int, perpage: int) -> Dict[str, Any]:
    payload = await kino_get(session, 'v1/history', {'page': api_page, 'perpage': perpage})
    entries = payload.get('history') if isinstance(payload, dict) else None
    return {'entries': entries if isinstance(entries, list) else [], 'payload': payload}


async def _history_scan(session: Dict[str, Any], sid: str, section: str) -> List[Dict[str, Any]]:
    """Every history item of one type (or one of the two aggregate groups
    above), walking upstream pages until exhausted."""
    types = HISTORY_GROUPS.get(section) or {section}
    key = f'{hashlib.sha256(sid.encode("utf-8")).hexdigest()[:16]}:{section}'
    cached = history_cache.get(key)
    if cached and cached['at'] + HISTORY_CACHE_TTL > time.time():
        return cached['items']
    collected: List[Dict[str, Any]] = []
    scanned = 0
    for api_page in range(1, HISTORY_SCAN_PAGES + 1):
        result = await _history_fetch(session, api_page, HISTORY_SCAN_PERPAGE)
        entries = result['entries']
        scanned += len(entries)
        for entry in entries:
            item = _history_entry_item(entry)
            if item and str(item.get('type') or '') in types:
                collected.append(item)
        if len(entries) < HISTORY_SCAN_PERPAGE:
            break
    history_cache[key] = {'at': time.time(), 'items': collected}
    log_event('catalog', 'History scanned for a type filter', {
        'type': section, 'matched': len(collected), 'scanned': scanned,
    })
    return collected



@app.get('/catalog/history')
async def catalog_history(page: int = 0, perpage: int = 48, type: str = '', kp_session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    """Viewing history from KinoPub, newest first, grouped by day.

    Distinct from ``/history``, which is this bridge's own resume positions.
    """
    section = (type or '').strip().lower()
    if section and section not in HISTORY_TYPES and section not in HISTORY_GROUPS:
        raise HTTPException(400, f'Unknown history type: {section}')
    page = max(0, min(page, 9999))
    perpage = max(1, min(perpage, 100))
    session = await refresh_if_needed(kp_session or '', session_get(kp_session))

    if section:
        matched = await _history_scan(session, kp_session or '', section)
        start = page * perpage
        items = matched[start:start + perpage]
        total_items = len(matched)
        total_pages = max(1, (total_items + perpage - 1) // perpage)
        log_event('catalog', 'History loaded', {
            'page': page, 'type': section, 'count': len(items), 'total': total_items,
        })
        return {
            'page': page, 'perpage': perpage, 'type': section,
            'total_pages': total_pages, 'total_items': total_items,
            'has_next': start + perpage < total_items,
            'items': items,
        }

    result = await _history_fetch(session, page + 1, perpage)
    entries = result['entries']
    items = [item for item in (_history_entry_item(entry) for entry in entries) if item]
    totals = _pagination_values(result['payload'], perpage)
    log_event('catalog', 'History loaded', {
        'page': page, 'type': 'all', 'count': len(items), 'raw': len(entries),
    })
    return {
        'page': page,
        'perpage': perpage,
        'type': section,
        'total_pages': totals['total_pages'],
        'total_items': totals['total_items'],
        'has_next': (page + 1 < totals['total_pages']) if totals['total_pages'] > 1 else (len(entries) >= perpage),
        'items': items,
    }


@app.get('/catalog/watching')
async def catalog_watching(kp_session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    """"Я смотрю" ("I'm watching") - series explicitly marked "Буду смотреть"
    (subscribed via `v1/watching/togglewatchlist`), straight from KinoPub's
    own `v1/watching/serials?subscribed=1` (a real, dedicated endpoint - not
    something built by scanning /catalog/history, which would have meant
    "everything ever watched", nor the unfiltered `v1/watching/serials`
    list, which is "every tracked serial with new episodes" regardless of
    whether it was ever subscribed to). Each entry carries real
    total/watched/new episode counts.
    """
    session = await refresh_if_needed(kp_session or '', session_get(kp_session))
    payload = await kino_get(session, 'v1/watching/serials', {'subscribed': 1})
    raw_items = payload.get('items') if isinstance(payload, dict) else None
    items: List[Dict[str, Any]] = []
    for raw in (raw_items or []):
        if not isinstance(raw, dict):
            continue
        item = normalize_catalog_item(raw)
        # This list is itself the `subscribed=1` filter - the raw entries
        # here don't carry their own `subscribed` field the way a single
        # item's detail fetch does, so normalize_catalog_item would default
        # it to False despite every entry being subscribed by construction.
        item['subscribed'] = True
        item['watching_total'] = int(_plain_number(raw.get('total')) or 0)
        item['watching_watched'] = int(_plain_number(raw.get('watched')) or 0)
        item['watching_new'] = int(_plain_number(raw.get('new')) or 0)
        items.append(item)
    log_event('catalog', 'Watching list loaded', {'count': len(items)})
    return {'items': items, 'total_items': len(items)}


SERIAL_TYPES = {'serial', 'docuserial', 'tvshow'}
# Deeper than HISTORY_SCAN_PAGES (20), which exists for the history *type
# filter* where a partial scan just means a shorter list. Here a page not
# scanned is a subscription silently missing, so this walks until the history
# runs out; the cap is only a runaway guard. At 50 per page that is 15 000
# entries - the account this was built against exhausts at 44 pages (~2 150
# entries) in about 7 seconds, and the result is cached.
SUBSCRIBED_SCAN_PAGES = 300
SUBSCRIBED_CACHE_TTL = 300
subscribed_cache: Dict[str, Dict[str, Any]] = {}


@app.get('/catalog/watching/subscribed')
async def catalog_watching_subscribed(kp_session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    """Every serial marked "Буду смотреть", including the fully-watched ones.

    `/catalog/watching` cannot answer this and no amount of parameters will
    make it. `v1/watching/serials` is titled, in KinoPub's own docs, "Список
    сериалов с новыми/не досмотренными сериями" - its domain is serials with
    something left to watch, and `subscribed=1` only narrows *within* that.
    Verified live against an account with four subscriptions: it returned two
    (the two with unwatched episodes), and the other two were absent from the
    unfiltered 28-entry list as well, because they are fully watched. A sweep
    for a list endpoint (`v1/watchlist`, `v1/user/watchlist`,
    `v1/watching/list`, `v1/watching/watchlist`, `v1/watching/serials/
    subscribed`, `v1/bookmarks/watchlist`) returned 404 for every one, and
    `subscribed`/`watchlist`/`finished` as `v1/items` params are ignored
    outright.

    What does exist: **every `v1/history` entry embeds the whole item, and
    that item carries `subscribed`/`in_watchlist`** (verified live - Grey's
    Anatomy and Rooster both came back `subscribed=True` from a history page,
    with no per-item fetch). So the list is assembled as:

        serials from `v1/watching/serials?subscribed=1`   (subscribed, unfinished)
      ∪ serials in the watch history flagged `subscribed`  (subscribed, finished)

    which is complete by construction: a subscribed serial either still has
    something unwatched (first set) or has been watched, and being watched is
    what puts it in the history (second set). The only real limit is scan
    depth - `HISTORY_SCAN_PAGES` pages back - and that is reported honestly in
    the response as `scanned_pages`/`history_exhausted` rather than hidden.
    """
    session = await refresh_if_needed(kp_session or '', session_get(kp_session))
    cache_key = hashlib.sha256((kp_session or '').encode('utf-8')).hexdigest()[:16]
    cached = subscribed_cache.get(cache_key)
    if cached and cached['at'] + SUBSCRIBED_CACHE_TTL > time.time():
        return cached['result']

    payload = await kino_get(session, 'v1/watching/serials', {'subscribed': 1})
    items: List[Dict[str, Any]] = []
    seen: set = set()
    for raw in (payload.get('items') or []) if isinstance(payload, dict) else []:
        if not isinstance(raw, dict):
            continue
        item = normalize_catalog_item(raw)
        if not item['id'] or item['id'] in seen:
            continue
        item['subscribed'] = True
        item['watching_total'] = int(_plain_number(raw.get('total')) or 0)
        item['watching_watched'] = int(_plain_number(raw.get('watched')) or 0)
        item['watching_new'] = int(_plain_number(raw.get('new')) or 0)
        seen.add(item['id'])
        items.append(item)

    scanned_pages = 0
    exhausted = False
    for api_page in range(1, SUBSCRIBED_SCAN_PAGES + 1):
        result = await _history_fetch(session, api_page, HISTORY_SCAN_PERPAGE)
        entries = result['entries']
        scanned_pages += 1
        for entry in entries:
            raw = entry.get('item') if isinstance(entry, dict) and isinstance(entry.get('item'), dict) else None
            if not raw or not (raw.get('subscribed') or raw.get('in_watchlist')):
                continue
            if str(raw.get('type') or '') not in SERIAL_TYPES:
                continue
            item = _history_entry_item(entry)
            if not item or item['id'] in seen:
                continue
            item['subscribed'] = True
            # Nothing left unwatched - that is precisely why this one was not
            # in the watching list. Stated rather than left absent so the UI
            # does not have to guess what a missing count means.
            item['watching_new'] = 0
            # `_history_entry_item` describes *a viewing*, so it carries the
            # episode that was watched (season/episode/frame/media title). On
            # this screen the card is the serial, not the last episode of it -
            # left in, half the grid captioned itself "S22E18 · Bridge Over
            # Troubled Liquor" while the rest showed the show's own name.
            for field in ('history_season', 'history_episode', 'media_title',
                          'episode_thumbnail', 'watched_at'):
                item.pop(field, None)
            seen.add(item['id'])
            items.append(item)
        if len(entries) < HISTORY_SCAN_PERPAGE:
            exhausted = True
            break

    log_event('catalog', 'Subscribed serials assembled', {
        'count': len(items), 'scanned_pages': scanned_pages, 'history_exhausted': exhausted,
    })
    result = {
        'items': items, 'total_items': len(items),
        'scanned_pages': scanned_pages, 'history_exhausted': exhausted,
    }
    subscribed_cache[cache_key] = {'at': time.time(), 'result': result}
    return result


@app.get('/catalog/items/{item_id}/similar')
async def catalog_item_similar(item_id: str, kp_session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    """KinoPub's own "похожие" list for one title (`v1/items/similar?id=`).

    A real, dedicated endpoint - it validates like one (400 without `id`, 404
    for an id that does not exist) rather than quietly ignoring the parameter
    the way several other "obvious" filters do.

    **It is genuinely empty for most of the catalogue**, which the caller has
    to handle rather than treat as an error. Measured live over 60 titles:
    fresh films 3/15, fresh serials 1/15, most-viewed films 7/15, the oldest
    serials 9/15 - so roughly a third overall, skewed towards older and
    popular entries. "Дом дракона" returns nothing at all even though
    kino.pub's own site shows a Похожие block for it, so the site is filling
    that from something other than this endpoint. Rather than invent a
    genre-based stand-in and pass it off as KinoPub's recommendations, this
    returns exactly what the API gives and the UI hides the section when the
    list comes back empty.
    """
    session = await refresh_if_needed(kp_session or '', session_get(kp_session))
    payload = await kino_get(session, 'v1/items/similar', {'id': item_id})
    raw_items = payload.get('items') if isinstance(payload, dict) else None
    items = [normalize_catalog_item(raw) for raw in (raw_items or []) if isinstance(raw, dict)]
    items = [item for item in items if item['id']]
    log_event('catalog', 'Similar titles loaded', {'item_id': item_id, 'count': len(items)})
    return {'items': items, 'total_items': len(items)}


@app.get('/catalog/tv')
async def catalog_tv(kp_session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    """Live TV channels - a real, separate KinoPub feature from the VOD
    catalogue (`v1/tv`, kinoapi.com/api_tv.html), not the mock feed the
    "Спорт" sidebar section used before (a VOD genre filter that returned
    movies/series, not the actual live channel list kino.watch's own
    "Спортивные трансляции" page shows there). Verified live: every channel
    this account's `v1/tv` returns is sport (ESPN, Eurosport, Fox Sports,
    TNT Sport UHD, MATCH!-branded channels...) and matches that page's own
    channel list one-for-one, so no further genre filtering is needed here.
    """
    session = await refresh_if_needed(kp_session or '', session_get(kp_session))
    payload = await kino_get(session, 'v1/tv', {})
    raw_channels = payload.get('channels') if isinstance(payload, dict) else None
    channels: List[Dict[str, Any]] = []
    for raw in (raw_channels or []):
        if not isinstance(raw, dict):
            continue
        stream = str(raw.get('stream') or '').strip()
        if not stream:
            continue
        channels.append({
            'id': str(raw.get('id', '')),
            'name': str(raw.get('name') or ''),
            'title': str(raw.get('title') or '').strip(),
            'logo': _image_url(raw.get('logos')),
            'stream': stream,
        })
    log_event('catalog', 'TV channels loaded', {'count': len(channels)})
    return {'channels': channels}


@app.get('/catalog/genres')
async def catalog_genres(content_type: str = 'movie', kp_session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    """Real genre reference list (`v1/genres?type=`) for the filter panel -
    replaces the old empty stub `<select>` that had no options at all."""
    cache_key = 'genres:' + (content_type or 'all')
    cached = kv_get(cache_key)
    if cached is not None:
        return {'genres': cached}
    session = await refresh_if_needed(kp_session or '', session_get(kp_session))
    payload = await kino_get(session, 'v1/genres', {'type': content_type} if content_type else {})
    raw_items = payload.get('items') if isinstance(payload, dict) else None
    genres = [{'id': r.get('id'), 'title': str(r.get('title') or '')} for r in (raw_items or []) if isinstance(r, dict) and r.get('id') is not None]
    if genres:
        kv_set(cache_key, genres, REFERENCE_TTL)
    return {'genres': genres}


@app.get('/catalog/countries')
async def catalog_countries(kp_session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    """Real country reference list (`v1/countries`) for the filter panel."""
    cached = kv_get('countries')
    if cached is not None:
        return {'countries': cached}
    session = await refresh_if_needed(kp_session or '', session_get(kp_session))
    payload = await kino_get(session, 'v1/countries', {})
    raw_items = payload.get('items') if isinstance(payload, dict) else (payload if isinstance(payload, list) else None)
    countries = [{'id': r.get('id'), 'title': str(r.get('title') or '')} for r in (raw_items or []) if isinstance(r, dict) and r.get('id') is not None]
    if countries:
        kv_set('countries', countries, REFERENCE_TTL)
    return {'countries': countries}


@app.get('/catalog/bookmarks')
async def catalog_bookmarks(kp_session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    """Real per-account bookmark folders (`v1/bookmarks`,
    kinoapi.com/api_bookmarks.html) - the "Закладки" sidebar button was dead
    (no `data-route`) before this. Browsing only for now: creating/renaming/
    deleting folders and adding/removing items are real documented endpoints
    too (`v1/bookmarks/create`, `/add`, `/remove-folder`, `/remove-item`,
    `/toggle-item`) but weren't asked for, so not wired up.
    """
    session = await refresh_if_needed(kp_session or '', session_get(kp_session))
    payload = await kino_get(session, 'v1/bookmarks', {})
    raw_items = payload.get('items') if isinstance(payload, dict) else None
    folders = []
    for r in (raw_items or []):
        if not isinstance(r, dict) or r.get('id') is None:
            continue
        folders.append({
            'id': str(r.get('id')),
            'title': str(r.get('title') or ''),
            'count': int(_plain_number(r.get('count')) or 0),
            'views': int(_plain_number(r.get('views')) or 0),
            'updated': r.get('updated'),
        })
    return {'folders': folders}


@app.get('/catalog/bookmarks/{folder_id}')
async def catalog_bookmark_folder(folder_id: str, page: int = 0, perpage: int = 48, kp_session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    """One bookmark folder's contents (`v1/bookmarks/view?folder=`) - same
    item shape as the regular catalogue, so the existing card/details flow
    works unchanged.

    Deliberately does NOT go through `extract_catalog_items()` here, unlike
    every other list endpoint. That function recursively walks the whole
    payload and dedupes by id, which is right when items can plausibly
    appear more than once *from the shape of the walk itself* (nested under
    several keys). A bookmark folder is already a flat `items` array, so a
    repeated id there means the account genuinely bookmarked the same title
    twice - confirmed live on this account's own folder (`Тор: Любовь и гром`
    appears twice, real duplicate entries, not a parsing artifact). Deduping
    silently showed 4 cards under a folder whose own `count` said 5, which
    is exactly the mismatch that got reported. Keeping every entry (even a
    literal duplicate) makes the grid match the folder's own count number.
    """
    session = await refresh_if_needed(kp_session or '', session_get(kp_session))
    api_page = max(0, page) + 1
    perpage = max(1, min(perpage, 100))
    payload = await kino_get(session, 'v1/bookmarks/view', {'folder': folder_id, 'page': api_page, 'perpage': perpage})
    raw_folder = payload.get('folder') if isinstance(payload, dict) else {}
    raw_items = payload.get('items') if isinstance(payload, dict) else None
    items = [normalize_catalog_item(r) for r in (raw_items or []) if isinstance(r, dict)]
    totals = _pagination_values(payload, perpage)
    return {
        'folder': {'id': folder_id, 'title': str((raw_folder or {}).get('title') or '')},
        'page': page,
        'perpage': perpage,
        'total_items': totals['total_items'],
        'total_pages': totals['total_pages'],
        'has_next': (page + 1 < totals['total_pages']) if totals['total_pages'] > 1 else (len(items) >= perpage),
        'items': items,
    }


COLLECTION_SORTS = {'new': 'updated-', 'top': 'watchers-', 'views': 'views-'}


def _normalize_collection(raw: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'id': str(raw.get('id', '')).strip(),
        'title': str(raw.get('title') or ''),
        'watchers': int(_plain_number(raw.get('watchers')) or 0),
        'views': int(_plain_number(raw.get('views')) or 0),
        'updated': raw.get('updated'),
        'poster': _image_url(raw.get('posters'), 'big'),
    }


@app.get('/catalog/collections')
async def catalog_collections(
    sort: str = 'new', page: int = 0, perpage: int = 24,
    kp_session: Optional[str] = Cookie(default=None),
) -> Dict[str, Any]:
    """Curated "Подборки" (`v1/collections`, kinoapi.com/api_collections.html
    - documented on its own page, separate from the general video-API
    reference, which is why an earlier session missed it and flagged the
    sidebar button as merely "confirmed-real, unused").

    `sort` here is one of three named tabs, not a raw passthrough:
    kino.watch's own page has five (Новые/Популярные/Просматриваемые/
    Категории/Подписки), and only the first three map onto a real
    `v1/collections` sort value - `Категории` groups by genre (a different
    shape of listing, not a sort) and `Подписки` is "collections *this
    account* follows" (no subscribe/list-subscriptions endpoint is
    documented, unlike the item watchlist's `togglewatchlist`). Verified
    live that the three that do exist are not decorative: `sort=updated-`
    (default), `watchers-`, and `views-` each return a genuinely different
    first page, matching kino.watch's own "Новые"/"Популярные"/
    "Просматриваемые" tabs item-for-item on this account (id 33 "MARVEL"
    first under `watchers-` with `watchers=2131`, exactly what the site's
    own "Популярные" tab shows).
    """
    upstream_sort = COLLECTION_SORTS.get(sort, COLLECTION_SORTS['new'])
    session = await refresh_if_needed(kp_session or '', session_get(kp_session))
    perpage = max(1, min(perpage, 100))
    api_page = max(0, page) + 1
    payload = await kino_get(session, 'v1/collections', {'sort': upstream_sort, 'page': api_page, 'perpage': perpage})
    raw_items = payload.get('items') if isinstance(payload, dict) else None
    collections = [_normalize_collection(r) for r in (raw_items or []) if isinstance(r, dict) and r.get('id') is not None]
    totals = _pagination_values(payload, perpage)
    return {
        'items': collections, 'page': page, 'perpage': perpage,
        'total_items': totals['total_items'], 'total_pages': totals['total_pages'],
        'has_next': (page + 1 < totals['total_pages']) if totals['total_pages'] > 1 else (len(collections) >= perpage),
    }


@app.get('/catalog/collections/{collection_id}')
async def catalog_collection_view(collection_id: str, kp_session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    """One collection's contents (`v1/collections/view?id=`) - same item
    shape as the regular catalogue (verified live: same fields as a
    `v1/items` entry, movies and serials mixed freely, e.g. "MARVEL" opens
    with two serials before its first movie), so the existing card/details
    flow needs no changes to show them.

    No `page`/`perpage` here because upstream has none for this endpoint -
    `v1/collections/view` returns every item in one response (confirmed live:
    67 items for "MARVEL" in a single reply, no `pagination` key at all,
    matching the "Фильмов 67" count kino.watch's own page shows). The
    frontend paginates this list client-side rather than pretend the API
    offers server paging it does not.
    """
    session = await refresh_if_needed(kp_session or '', session_get(kp_session))
    payload = await kino_get(session, 'v1/collections/view', {'id': collection_id})
    raw_collection = payload.get('collection') if isinstance(payload, dict) else {}
    raw_items = payload.get('items') if isinstance(payload, dict) else None
    items = [normalize_catalog_item(r) for r in (raw_items or []) if isinstance(r, dict)]
    collection = _normalize_collection(raw_collection if isinstance(raw_collection, dict) else {'id': collection_id})
    return {'collection': collection, 'items': items, 'total_items': len(items)}


@app.get('/catalog/autocomplete')
async def catalog_autocomplete(q: str) -> Dict[str, Any]:
    query = q.strip()
    if len(query) < 2:
        return {'items': []}
    try:
        upstream = await app.state.http.get(
            'https://api.kinopub.link/v1.1/autocomplete',
            params={'query': query},
            headers={'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0 KinoPub-webOS/0.9.9'},
        )
        upstream.raise_for_status()
        payload = upstream.json()
    except (httpx.HTTPError, ValueError) as exc:
        log_event('search', 'Autocomplete failed', {'query': query, 'error': str(exc)})
        return {'items': []}
    raw_items = (payload.get('items') or payload.get('data') or payload.get('results')) if isinstance(payload, dict) else payload
    if not isinstance(raw_items, list):
        raw_items = []
    items = []
    for raw in raw_items[:25]:
        if isinstance(raw, str):
            items.append({'id': '', 'value': raw})
            continue
        if not isinstance(raw, dict):
            continue
        nested = raw.get('item') if isinstance(raw.get('item'), dict) else {}
        item_id = raw.get('id') or raw.get('item_id') or raw.get('mid') or nested.get('id') or ''
        value = raw.get('value') or raw.get('title') or raw.get('name') or nested.get('title') or ''
        if value:
            items.append({'id': str(item_id), 'value': str(value)})
    return {'items': items}


SEARCH_MODE_FIELDS = {'title': 'title', 'actor': 'cast', 'director': 'director'}


@app.get('/catalog/search')
async def catalog_search(q: str, mode: str = 'all', kp_session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    """`v1/items/search`'s real `field` parameter - documented (only on
    `api_video.html`'s search section, easy to miss) as "поиск только в одном
    из полей title,director,cast" - was never wired here, so "Актёры"/
    "Режиссёры" search mode always ran the exact same all-fields query as
    "Все" and the title-only client-side narrowing above stood in for
    `field=title`. Found while wiring the details screen's own director/cast
    badges to this same search: clicking "Дени Вильнёв" (director of "Дюна:
    Часть вторая") returned zero results, because a person's name searched
    against an all-fields/title-shaped result is not the same as searching
    it *as a director*. Verified live: `field=director` for that exact name
    returns 13 real title, `field=cast` for a cast member returns 37 - the
    parameter genuinely narrows, it was just never sent.
    """
    query = q.strip()
    if not query:
        return {'query': '', 'mode': mode, 'items': []}
    session = await refresh_if_needed(kp_session or '', session_get(kp_session))
    params: Dict[str, Any] = {'q': query, 'page': 0, 'perpage': 60}
    field = SEARCH_MODE_FIELDS.get(mode)
    if field:
        params['field'] = field
    payload = await kino_get(session, 'v1/items/search', params)
    items = extract_catalog_items(payload)
    return {'query': query, 'mode': mode, 'items': items}


def _id_name_list(value: Any) -> List[Dict[str, Any]]:
    """Like `_name_list`, but keeps the id when the payload actually has one.

    Needed anywhere a genre/country badge on the details screen has to link
    to a real filter - the filter panel filters `v1/items` by numeric id, not
    by title text, and a comma-separated-string payload (or a plain string
    list) simply has no id to give. Those entries get `id: None` and the
    frontend renders them as plain text rather than a link that would filter
    on nothing.
    """
    out: List[Dict[str, Any]] = []
    if isinstance(value, list):
        for entry in value:
            if isinstance(entry, dict):
                title = str(entry.get('title') or entry.get('name') or '').strip()
                if title:
                    out.append({'id': entry.get('id'), 'title': title})
            elif entry:
                title = str(entry).strip()
                if title:
                    out.append({'id': None, 'title': title})
    elif isinstance(value, str):
        for part in value.split(','):
            title = part.strip()
            if title:
                out.append({'id': None, 'title': title})
    return out


def _name_list(value: Any) -> List[str]:
    """Flatten KinoPub's people/country fields into plain names.

    They arrive as a comma-separated string in some payloads and as a list of
    ``{id, title}`` objects in others.
    """
    names: List[str] = []
    if isinstance(value, str):
        names = [part.strip() for part in value.split(',')]
    elif isinstance(value, list):
        for entry in value:
            if isinstance(entry, dict):
                names.append(str(entry.get('title') or entry.get('name') or '').strip())
            elif entry:
                names.append(str(entry).strip())
    return [name for name in names if name]


def _vote_counts(raw: Dict[str, Any]) -> Dict[str, int]:
    positive = _plain_number(_pick_first(raw, ['positive', 'rating_positive', 'votes_positive', 'likes']))
    negative = _plain_number(_pick_first(raw, ['negative', 'rating_negative', 'votes_negative', 'dislikes']))
    if positive is None and negative is None:
        # Only the net balance and a total are exposed: recover both halves.
        net = _plain_number(raw.get('rating'))
        total = _plain_number(_pick_first(raw, ['rating_votes', 'votes_total', 'votes_count', 'vote_count']))
        if net is not None and total and total > 0 and abs(net) <= total:
            positive = (total + net) / 2.0
            negative = total - positive
    return {
        'positive': int(positive) if positive is not None else 0,
        'negative': int(negative) if negative is not None else 0,
    }


def _item_details(raw: Dict[str, Any], media: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The extra fields the details panel shows beyond a catalogue card.

    ``duration`` here is the **total** across every entry under `videos`/
    `episodes` (KinoPub's own `duration.total`) - genuinely meaningful for a
    series, where it is the sum of real episodes. For a movie it is not: a
    movie's `videos` array holds *alternate versions* of the same film, not a
    sequence (verified live on "Дюна: Часть вторая" - two entries, "24 fps"
    and "48 fps", `duration.total` = their sum = ~5h33m for a ~2h47m movie).
    Summing alternate cuts is meaningless, so the frontend never uses this
    field for a movie - it takes the first entry's own duration instead
    (`duration_average`, KinoPub's `duration.average`, is exposed alongside
    for the same reason: only meaningful when the entries are actually
    episodes of one series, not different renders of the same one).
    """
    duration = _plain_number(_nested_get(raw, 'duration.total')) or _plain_number(_pick_first(raw, ['duration', 'length']))
    duration_average = _plain_number(_nested_get(raw, 'duration.average'))
    subtitle_langs: List[str] = []
    audio_langs: List[str] = []
    for entry in media:
        for subtitle in entry.get('subtitles') or []:
            name = _pick_first(subtitle, ['language_name', 'language', 'lang'], '') if isinstance(subtitle, dict) else ''
            if name and str(name) not in subtitle_langs:
                subtitle_langs.append(str(name))
        for audio in entry.get('audios') or []:
            name = _pick_first(audio, ['language_name', 'language', 'lang'], '') if isinstance(audio, dict) else ''
            if name and str(name) not in audio_langs:
                audio_langs.append(str(name))
    seasons = {str(entry.get('season') or 1) for entry in media}
    return {
        'countries': _name_list(raw.get('countries') or raw.get('country')),
        # `_detailed` pairs feed the clickable genre/country badges on the
        # details screen (id -> real filter, matching kino.watch's own
        # ?genre=/?country= links); the plain string lists above stay as they
        # were for the catalogue card and anything that just wants text.
        'countries_detailed': _id_name_list(raw.get('countries') or raw.get('country')),
        'genres_detailed': _id_name_list(raw.get('genres')),
        'director': ', '.join(_name_list(raw.get('director') or raw.get('directors'))),
        'directors': _name_list(raw.get('director') or raw.get('directors')),
        'cast': _name_list(raw.get('cast') or raw.get('actors')),
        'duration': int(duration) if duration else 0,
        'duration_average': int(duration_average) if duration_average else 0,
        'quality': str(_pick_first(raw, ['quality', 'max_quality'], '') or ''),
        'votes': _vote_counts(raw),
        'imdb_votes': int(_plain_number(_pick_first(raw, ['imdb_votes', 'imdb_vote', 'votes_imdb'])) or 0),
        'kinopoisk_votes': int(_plain_number(_pick_first(raw, ['kinopoisk_votes', 'kinopoisk_vote', 'votes_kinopoisk'])) or 0),
        'subtitle_langs': subtitle_langs,
        'audio_langs': audio_langs,
        'seasons_count': len(seasons) if len(media) > 1 else 0,
        'episodes_count': len(media) if len(media) > 1 else 0,
        'updated_at': int(_plain_number(_pick_first(raw, ['updated', 'updated_at', 'created'])) or 0),
        'finished': bool(raw.get('finished')) if raw.get('finished') is not None else None,
    }


@app.get('/catalog/items/{item_id}')
async def catalog_item(item_id: str, kp_session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    session = await refresh_if_needed(kp_session or '', session_get(kp_session))
    payload = await kino_get(session, f'v1/items/{item_id}', {})
    raw_item = payload.get('item') if isinstance(payload, dict) and isinstance(payload.get('item'), dict) else payload
    item = normalize_catalog_item(raw_item if isinstance(raw_item, dict) else {'id': item_id})
    media = collect_media(payload)
    item.update(_item_details(raw_item if isinstance(raw_item, dict) else {}, media))
    item['media'] = media
    seasons: Dict[str, Dict[str, Any]] = {}
    for entry in media:
        season_no = str(entry.get('season') or 1)
        seasons.setdefault(season_no, {'number': entry.get('season') or 1, 'episodes': []})['episodes'].append(entry)
    item['seasons'] = sorted(seasons.values(), key=lambda s: _plain_number(s['number']) or 0)
    return item


@app.post('/catalog/items/{item_id}/vote')
async def catalog_item_vote(item_id: str, like: int = 1, kp_session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    """Cast a thumbs up/down vote. KinoPub's own vote endpoint is a GET with
    query params (`v1/items/vote?id=&like=`) - exposed here as a POST since
    it's a mutation from our side, regardless of how upstream models it."""
    session = await refresh_if_needed(kp_session or '', session_get(kp_session))
    payload = await kino_get(session, 'v1/items/vote', {'id': item_id, 'like': 1 if like else 0})
    return {
        'voted': bool(payload.get('voted')),
        'positive': int(_plain_number(payload.get('positive')) or 0),
        'negative': int(_plain_number(payload.get('negative')) or 0),
        'rating': int(_plain_number(payload.get('rating')) or 0),
    }


@app.post('/catalog/items/{item_id}/watchlist')
async def catalog_item_watchlist(item_id: str, kp_session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    """Toggle "Буду смотреть" tracking for a title via KinoPub's real
    `v1/watching/togglewatchlist?id=` - flips membership in the same list
    `/catalog/watching` (`v1/watching/serials?subscribed=1`) reads from.
    Verified live: confirmed request/response shape and that it round-trips
    (toggled off and back on the same item without leaving state changed)."""
    session = await refresh_if_needed(kp_session or '', session_get(kp_session))
    payload = await kino_get(session, 'v1/watching/togglewatchlist', {'id': item_id})
    return {'subscribed': bool(payload.get('watching'))}


def _watching_episode_map(raw_item: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Flatten `v1/watching?id=`'s per-video status into ``{video_id: {...}}``.

    A movie's `item` has a flat `videos` list; a series nests episodes under
    `seasons[].episodes[]` instead - there's no one shape to walk. `status`
    is -1 (never watched) / 0 (in progress) / 1 (watched) - a bare
    `bool(status)` would treat -1 as truthy and mark everything watched.
    """
    videos: List[Dict[str, Any]] = [v for v in (raw_item.get('videos') or []) if isinstance(v, dict)]
    for season in (raw_item.get('seasons') or []):
        if isinstance(season, dict):
            videos.extend(v for v in (season.get('episodes') or []) if isinstance(v, dict))
    episodes: Dict[str, Dict[str, Any]] = {}
    for video in videos:
        video_id = str(video.get('id') or '')
        if not video_id:
            continue
        episodes[video_id] = {
            'watched': video.get('status') == 1,
            'position': int(_plain_number(video.get('time')) or 0),
            'duration': int(_plain_number(video.get('duration')) or 0),
        }
    return episodes


@app.get('/catalog/items/{item_id}/watching')
async def catalog_item_watching(item_id: str, kp_session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    """KinoPub's own watched status for one title, straight from `v1/watching
    ?id=` - a real per-video `status`/`time` (position), tracked across every
    device the account has used, not just this client's local SQLite
    progress. Cheap for a single title; unlike /watching/statuses (which
    scans up to 20 pages of v1/history for the whole catalogue grid at once),
    this has no bulk equivalent - it only ever takes one id."""
    session = await refresh_if_needed(kp_session or '', session_get(kp_session))
    payload = await kino_get(session, 'v1/watching', {'id': item_id})
    raw_item = payload.get('item') if isinstance(payload, dict) else {}
    if not isinstance(raw_item, dict):
        raw_item = {}
    return {
        'watched': raw_item.get('status') == 1,
        'episodes': _watching_episode_map(raw_item),
    }


@app.get('/catalog/items/{item_id}/play')
async def catalog_play(item_id: str, media_id: Optional[str] = None, kp_session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    session = await refresh_if_needed(kp_session or '', session_get(kp_session))
    payload = await kino_get(session, f'v1/items/{item_id}', {})
    media = collect_media(payload)
    selected = next((entry for entry in media if media_id and entry['id'] == media_id), None) or (media[0] if media else None)
    if not selected:
        raise HTTPException(404, 'No playable media found for this item')
    streams = list(selected.get('streams') or [])
    subtitles = list(selected.get('subtitles') or [])
    audios = list(selected.get('audios') or [])
    expected = expected_track_count(selected.get('tracks'))
    # /v1/items/<id> frequently ships a trimmed video node that lists only the
    # default audio track, while media-links carries the full set. Ask for it
    # whenever the payload looks incomplete, not only when streams are missing,
    # and merge the result instead of replacing what we already have.
    incomplete = (not streams) or (not audios) or (expected > len(audios))
    if incomplete and not str(selected['id']).startswith('direct-'):
        try:
            links = await kino_get(session, 'v1/items/media-links', {'mid': selected['id']})
        except HTTPException as exc:
            links = {}
            log_event('media', 'media-links lookup failed', {'media_id': selected['id'], 'status': exc.status_code})
        if isinstance(links, dict):
            extra_streams: List[Dict[str, Any]] = []
            for file_value in (links.get('files') or []):
                if isinstance(file_value, dict):
                    extra_streams.extend(stream_from_file(file_value))
            merge_unique_list(streams, extra_streams, stream_key)
            merge_unique_list(audios, links.get('audios'), audio_key)
            merge_unique_list(subtitles, links.get('subtitles'), subtitle_key)
    audios = sorted_audios(audios)
    # Keep the embedded media node consistent with the top-level lists so the
    # player sees the same tracks regardless of which field it reads.
    selected['audios'] = audios
    selected['subtitles'] = subtitles
    selected['streams'] = streams
    best = choose_best_stream(streams)
    if not best:
        raise HTTPException(404, 'KinoPub returned no compatible stream URL')
    log_event('media', 'Play option resolved', {
        'item_id': item_id, 'media_id': selected['id'], 'protocol': best.get('source_type'),
        'quality': best.get('quality'), 'codec': best.get('codec'),
        'audio_count': len(audios), 'expected_tracks': expected, 'tracks': selected.get('tracks'),
        'subtitle_count': len(subtitles), 'enriched': incomplete,
    })
    return {
        'item_id': item_id, 'media': selected, 'streams': streams, 'selected': best,
        'subtitles': subtitles, 'audios': audios, 'expected_tracks': expected,
    }


POPULAR_SNAPSHOT_IDS = [
    '125428','124771','124450','125272','124756','125497','124447','124525','124621','124432',
    '124534','124600','124477','124459','125521','124072','125458','125038','124837','125035',
    '125170','125044','124921','124645','124483','124390','124279','124234','124255','124471'
]
HOT_SNAPSHOT_IDS = [
    '125821','125815','125728','125668','125662','125650','125647','125644','125623','125596',
    '125566','125524','125521','125503','125497','125458','125452','125446','125443','125428',
    '125425','125335','125326','125320','125272','125170','125137','125077','125074','125044'
]

def _ranking_score(actual_ids: List[str], target_ids: List[str]) -> Dict[str, Any]:
    actual = [str(x) for x in actual_ids[:len(target_ids)]]
    target = [str(x) for x in target_ids]
    target_pos = {item_id: index for index, item_id in enumerate(target)}
    overlap = [item_id for item_id in actual if item_id in target_pos]
    exact = sum(1 for index, item_id in enumerate(actual) if index < len(target) and item_id == target[index])
    displacement = sum(abs(index - target_pos[item_id]) for index, item_id in enumerate(actual) if item_id in target_pos)
    max_displacement = max(1, len(target) * len(target))
    order_component = max(0.0, 1.0 - displacement / max_displacement)
    overlap_ratio = len(overlap) / max(1, len(target))
    exact_ratio = exact / max(1, len(target))
    score = round((overlap_ratio * 0.65 + exact_ratio * 0.25 + order_component * 0.10) * 100, 2)
    return {
        'score': score,
        'overlap': len(overlap),
        'target_count': len(target),
        'exact_positions': exact,
        'first_ids': actual[:15],
        'missing_from_candidate': [x for x in target if x not in actual][:15],
    }

@app.get('/catalog/compare-feeds')
async def compare_catalog_feeds(feed: str = 'both', kp_session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    session = await refresh_if_needed(kp_session or '', session_get(kp_session))
    feed = (feed or 'both').strip().lower()
    if feed not in {'popular', 'hot', 'both'}:
        raise HTTPException(400, 'feed must be popular, hot or both')

    candidates = [
        {'name': 'shortcut_popular', 'path': 'v1/items/popular', 'params': {'type': 'movie', 'page': 0, 'perpage': 60}},
        {'name': 'shortcut_hot', 'path': 'v1/items/hot', 'params': {'type': 'movie', 'page': 0, 'perpage': 60}},
        {'name': 'items_views', 'path': 'v1/items', 'params': {'type': 'movie', 'sort': 'views-', 'page': 0, 'perpage': 60}},
        {'name': 'items_watchers', 'path': 'v1/items', 'params': {'type': 'movie', 'sort': 'watchers-', 'page': 0, 'perpage': 60}},
        {'name': 'items_rating', 'path': 'v1/items', 'params': {'type': 'movie', 'sort': 'rating-', 'page': 0, 'perpage': 60}},
        {'name': 'items_views_watchers', 'path': 'v1/items', 'params': {'type': 'movie', 'sort': 'views-,watchers-', 'page': 0, 'perpage': 60}},
        {'name': 'items_watchers_views', 'path': 'v1/items', 'params': {'type': 'movie', 'sort': 'watchers-,views-', 'page': 0, 'perpage': 60}},
        {'name': 'items_rating_views', 'path': 'v1/items', 'params': {'type': 'movie', 'sort': 'rating-,views-', 'page': 0, 'perpage': 60}},
        {'name': 'items_views_rating', 'path': 'v1/items', 'params': {'type': 'movie', 'sort': 'views-,rating-', 'page': 0, 'perpage': 60}},
    ]
    targets = {}
    if feed in {'popular', 'both'}:
        targets['popular'] = POPULAR_SNAPSHOT_IDS
    if feed in {'hot', 'both'}:
        targets['hot'] = HOT_SNAPSHOT_IDS

    results = []
    for candidate in candidates:
        try:
            payload = await kino_get(session, candidate['path'], candidate['params'])
            items = extract_catalog_items(payload)
            ids = [str(item.get('id')) for item in items if item.get('id')]
            scores = {name: _ranking_score(ids, target) for name, target in targets.items()}
            results.append({
                'candidate': candidate['name'],
                'path': candidate['path'],
                'params': candidate['params'],
                'count': len(ids),
                'scores': scores,
            })
        except HTTPException as exc:
            results.append({
                'candidate': candidate['name'],
                'path': candidate['path'],
                'params': candidate['params'],
                'error': {'status': exc.status_code, 'detail': str(exc.detail)[:500]},
            })

    best = {}
    for target_name in targets:
        eligible = [r for r in results if 'scores' in r and target_name in r['scores']]
        if eligible:
            winner = max(eligible, key=lambda r: r['scores'][target_name]['score'])
            best[target_name] = {
                'candidate': winner['candidate'],
                'path': winner['path'],
                'params': winner['params'],
                **winner['scores'][target_name],
            }
    log_event('catalog', 'Feed comparison completed', {'feed': feed, 'best': {k: v.get('candidate') for k, v in best.items()}})
    return {
        'snapshot': 'kino.watch saved pages, 2026-08-05',
        'feed': feed,
        'best': best,
        'results': results,
    }

@app.get('/health')
def health() -> Dict[str, Any]:
    return {
        'status': 'ok', 'version': app.version,
        'credentials_configured': bool(CLIENT_ID and CLIENT_SECRET),
        # The player reads this to skip the FFmpeg rung of the audio ladder
        # instead of offering a switch that will fail a second later.
        'ffmpeg': FFMPEG_AVAILABLE,
    }




def _subscription_payload(payload: Any) -> Dict[str, Any]:
    root = payload if isinstance(payload, dict) else {}
    user = root.get('user') if isinstance(root.get('user'), dict) else root
    subscription = user.get('subscription') if isinstance(user.get('subscription'), dict) else {}
    days_raw = subscription.get('days')
    end_raw = subscription.get('end_time')
    try:
        days = float(days_raw) if days_raw is not None else None
    except (TypeError, ValueError):
        days = None
    try:
        end_time = int(float(end_raw)) if end_raw is not None else None
    except (TypeError, ValueError):
        end_time = None
    now = int(time.time())
    if days is None and end_time is not None:
        days = max(0.0, (end_time - now) / 86400.0)
    active_raw = subscription.get('active')
    active = bool(active_raw) if active_raw is not None else bool(days is not None and days > 0)
    if end_time is not None and end_time <= now:
        active = False
    if days is not None and days <= 0:
        active = False
    return {
        'username': user.get('username'),
        'profile': user.get('profile') if isinstance(user.get('profile'), dict) else {},
        'subscription': {
            'active': active,
            'days': days,
            'end_time': end_time,
            'expired': not active,
        },
    }


@app.get('/profile')
async def profile(refresh: bool = False, kp_session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    sid = kp_session or ''
    session = await refresh_if_needed(sid, session_get(kp_session))
    cached = profile_cache.get(sid)
    if cached and not refresh and time.time() - cached.get('at', 0) < 300:
        return cached['data']
    try:
        payload = await kino_get(session, 'v1/user', {})
        data = _subscription_payload(payload)
        data['stale'] = False
        data['checked_at'] = int(time.time())
        profile_cache[sid] = {'at': time.time(), 'data': data}
        return data
    except Exception:
        if cached:
            stale = dict(cached['data'])
            stale['stale'] = True
            return stale
        raise

_device_registered_sids: set = set()


async def _ensure_device_registered(sid: str, session: Dict[str, Any]) -> None:
    """KinoPub shows this bridge as an "unknown"/"unknown"/"unknown" entry in
    the account's real device list (kino.pub -> Настройки -> Устройства),
    with 4K/HEVC support left at whatever that device record happened to
    default to - nothing ever called `v1/device/notify` or
    `v1/device/{id}/settings`. Verified live: each successful device-code
    pairing gets its own fresh device id from KinoPub (repeated pairings
    accumulate distinct ids, not one reused entry), so this is keyed to the
    bridge's own session id and only ever runs once per session, not on
    every request - `/auth/status` is polled far too often for a live
    KinoPub round-trip on each call.
    """
    if not sid or sid in _device_registered_sids:
        return
    _device_registered_sids.add(sid)
    try:
        info = await kino_get(session, 'v1/device/info', {})
        device = info.get('device') if isinstance(info, dict) else {}
        if not isinstance(device, dict):
            device = {}
        def _known(value: Any) -> bool:
            text = str(value or '').strip().lower()
            return bool(text) and text != 'unknown'
        if not (_known(device.get('title')) and _known(device.get('hardware'))):
            await kino_post(session, 'v1/device/notify', {
                'title': 'KinoPub webOS Bridge',
                'hardware': 'Web Browser',
                'software': f'kinopub-webos-client/{app.version}',
            })
        log_event('device', 'Device info registered with KinoPub', {'device_id': device.get('id')})
    except HTTPException as exc:
        _device_registered_sids.discard(sid)
        log_event('device', 'Device registration failed', {'status': exc.status_code})


def _device_flag(settings: Any, name: str) -> bool:
    entry = settings.get(name) if isinstance(settings, dict) else None
    return bool(entry.get('value')) if isinstance(entry, dict) else False


@app.post('/device/capabilities')
async def device_capabilities(payload: CapabilitiesPayload, kp_session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    """Tell KinoPub what this browser can actually decode.

    This is not cosmetic: KinoPub serves a **different set of files** per
    title depending on the device's `supportHevc`/`support4k` flags -
    verified live on one title by toggling the flags and re-reading
    `v1/items/{id}`:

        support4k=1 supportHevc=1 -> 2160p h265, 1080p h265, 720p, 480p
        support4k=0 supportHevc=1 -> 1080p h265, 720p, 480p
        support4k=0 supportHevc=0 -> 1080p h264, 720p, 480p

    So "open at the highest quality this device can play" is decided here,
    before the player ever sees a stream list: report the truth and the
    top entry KinoPub returns is playable by construction. Reporting a
    blanket `true` (what the previous version did) would hand an
    HEVC-incapable browser an HEVC-only list it cannot play at all.

    Written only when a flag actually differs, so the common case is one
    read and no write.

    **A flag the browser could not determine (``None``) is left alone.** The
    first version coerced every field to ``bool``, so "did not answer" landed
    on KinoPub as an explicit 0 - and `supportHevc=0` makes KinoPub serve a
    h264-only ladder for every title (verified live, see the matrix above),
    which removes the HEVC file, and with it the HDR file and any reason to
    play direct at all. On top of that a single device record is shared by
    every browser that talks to this bridge, so a desktop visit could quietly
    strip the TV's capabilities. Both failures are the same mistake: writing
    a negative that nobody actually asserted.
    """
    session = await refresh_if_needed(kp_session or '', session_get(kp_session))
    info = await kino_get(session, 'v1/device/info', {})
    device = info.get('device') if isinstance(info, dict) else {}
    if not isinstance(device, dict):
        device = {}
    device_id = device.get('id')
    settings = device.get('settings') if isinstance(device.get('settings'), dict) else {}
    known = {
        'supportHevc': payload.hevc,
        'support4k': payload.uhd,
        'supportHdr': payload.hdr,
    }
    desired = {name: bool(value) for name, value in known.items() if value is not None}
    skipped = sorted(name for name, value in known.items() if value is None)
    changed = {name: value for name, value in desired.items() if _device_flag(settings, name) != value}
    if device_id and changed:
        await kino_post(session, f'v1/device/{device_id}/settings', changed)
        log_event('device', 'Device capabilities updated', {'device_id': device_id, 'changed': changed})
    current = {name: _device_flag(settings, name) for name in known}
    current.update(desired)
    return {
        'device_id': device_id, 'applied': desired, 'skipped': skipped,
        'changed': sorted(changed), 'current': current,
    }


@app.get('/device/state')
async def device_state(kp_session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    """The capability flags KinoPub currently holds for this device.

    Read-only counterpart to `/device/capabilities`, for the diagnostics
    screen. These flags decide which files the API offers, and the last time
    they went wrong it cost a session to work out that the answer had been
    sitting in `v1/device/info` the whole time - so the player now shows them
    next to the browser's own probe results, on the TV itself.
    """
    session = await refresh_if_needed(kp_session or '', session_get(kp_session))
    info = await kino_get(session, 'v1/device/info', {})
    device = info.get('device') if isinstance(info, dict) else {}
    if not isinstance(device, dict):
        device = {}
    settings = device.get('settings') if isinstance(device.get('settings'), dict) else {}
    streaming = settings.get('streamingType') if isinstance(settings.get('streamingType'), dict) else {}
    selected = ''
    for entry in streaming.get('value') or []:
        if isinstance(entry, dict) and entry.get('selected'):
            selected = str(entry.get('label') or '')
            break
    return {
        'device_id': device.get('id'),
        'title': device.get('title'),
        'software': device.get('software'),
        'flags': {name: _device_flag(settings, name)
                  for name in ('supportHevc', 'support4k', 'supportHdr', 'supportSsl')},
        'streaming_type': selected,
    }


# --- CDN server selection -------------------------------------------------
#
# KinoPub hands out stream URLs from one of several CDN regions, chosen by the
# `serverLocation` entry of the account's *device* settings - the same record
# `/device/capabilities` already writes to. Verified live by flipping it and
# re-reading `v1/items/100468`:
#
#     serverLocation=1 (Нидерланды) -> <uuid>.ams-static-NN.cdntogo.net
#     serverLocation=3 (Россия)     -> <uuid>.msk-static-NN.cdntogo.net
#
# so the choice really does move the bytes, it is not a cosmetic label. The
# reference list is `v1/references/server-location`; it returned exactly those
# two entries, and this code reads it rather than hardcoding them so a third
# region appearing upstream needs no change here.
#
# Measurement is deliberately server-side. The bridge proxies HLS/relay
# playback itself, so the backend's own path to the CDN is the one that
# decides throughput for everything except `direct`, and it is the only path
# that can be measured honestly from here at all - the browser pane's route to
# `*.cdntogo.net` is known-broken in this sandbox while the container's works
# (see HANDOFF.md). What this does NOT measure is a TV's own direct-play route
# to the CDN; the frontend says so rather than implying otherwise.
SERVER_MEASURE_BYTES = 3 * 1024 * 1024
SERVER_MEASURE_BUDGET = 6.0
SERVER_MEASURE_TTL = 300
# One shared account-wide setting is being toggled to run this, so two
# overlapping runs would interleave their writes and could strand the account
# on whichever region lost the race. Serialised, never concurrent.
server_measure_lock = asyncio.Lock()
server_measure_last: Dict[str, Any] = {}


async def _device_record(session: Dict[str, Any]) -> Dict[str, Any]:
    info = await kino_get(session, 'v1/device/info', {})
    device = info.get('device') if isinstance(info, dict) else {}
    return device if isinstance(device, dict) else {}


def _selected_server_id(settings: Any) -> Optional[int]:
    entry = settings.get('serverLocation') if isinstance(settings, dict) else None
    for row in (entry or {}).get('value') or []:
        if isinstance(row, dict) and row.get('selected'):
            try:
                return int(row.get('id'))
            except (TypeError, ValueError):
                return None
    return None


def _server_options(settings: Any) -> List[Dict[str, Any]]:
    """Region list as the device record itself reports it.

    Preferred over `v1/references/server-location` because this copy already
    carries which one is selected, so the common read is a single call.
    """
    entry = settings.get('serverLocation') if isinstance(settings, dict) else None
    out: List[Dict[str, Any]] = []
    for row in (entry or {}).get('value') or []:
        if not isinstance(row, dict):
            continue
        try:
            out.append({'id': int(row.get('id')), 'name': str(row.get('label') or ''),
                        'selected': bool(row.get('selected'))})
        except (TypeError, ValueError):
            continue
    return out


@app.get('/servers')
async def servers(kp_session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    """Which CDN regions this account can pick from, and the current one."""
    session = await refresh_if_needed(kp_session or '', session_get(kp_session))
    device = await _device_record(session)
    settings = device.get('settings') if isinstance(device.get('settings'), dict) else {}
    options = _server_options(settings)
    return {
        'device_id': device.get('id'),
        'selected': _selected_server_id(settings),
        'options': options,
        'last_measured': server_measure_last or None,
    }


@app.post('/servers/select')
async def servers_select(payload: ServerSelectPayload, kp_session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    session = await refresh_if_needed(kp_session or '', session_get(kp_session))
    device = await _device_record(session)
    settings = device.get('settings') if isinstance(device.get('settings'), dict) else {}
    options = _server_options(settings)
    if payload.id not in {row['id'] for row in options}:
        raise HTTPException(400, f'Unknown server location id: {payload.id}')
    device_id = device.get('id')
    if not device_id:
        raise HTTPException(502, 'KinoPub did not report a device id')
    if _selected_server_id(settings) != payload.id:
        await kino_post(session, f'v1/device/{device_id}/settings', {'serverLocation': payload.id})
        log_event('device', 'CDN server switched', {'device_id': device_id, 'server': payload.id})
    return {'selected': payload.id, 'options': [
        {**row, 'selected': row['id'] == payload.id} for row in options]}


async def _probe_stream_url(session: Dict[str, Any], item_id: Optional[int]) -> Optional[str]:
    """A real, currently-valid stream URL to measure against.

    Has to be re-fetched for every region: the CDN host is baked into the
    signed URL, so reusing one across regions would measure the same box
    twice. Any playable item does, so the probe rides on whatever
    `v1/items/popular` returns first rather than pinning an id that may leave
    the catalogue.
    """
    if item_id is None:
        return None
    payload = await kino_get(session, f'v1/items/{item_id}', {})
    item = payload.get('item') if isinstance(payload, dict) else None
    videos = (item or {}).get('videos') or []
    files = (videos[0] or {}).get('files') if videos else []
    best = None
    for entry in files or []:
        if not isinstance(entry, dict):
            continue
        url = entry.get('url') or {}
        candidate = url.get('http') or url.get('hls4') or url.get('hls')
        if not candidate:
            continue
        best = best or candidate
        if entry.get('quality') == '1080p':
            return candidate
    return best


async def _connect_latency_ms(host: str) -> Optional[float]:
    """Best of three TCP+TLS handshakes.

    Not ICMP: the container has no raw sockets, and the number that actually
    matters for playback is how long opening a connection to that CDN edge
    takes anyway. Reported to the user as "отклик", never as "ping".
    """
    best: Optional[float] = None
    for _ in range(3):
        started = time.perf_counter()
        writer = None
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, 443, ssl=ssl.create_default_context(), server_hostname=host),
                timeout=8)
            elapsed = (time.perf_counter() - started) * 1000
            best = elapsed if best is None else min(best, elapsed)
        except Exception:
            return best
        finally:
            if writer is not None:
                writer.close()
                with suppress(Exception):
                    await writer.wait_closed()
    return best


async def _measure_download(client: httpx.AsyncClient, url: str) -> Dict[str, Any]:
    received = 0
    started = time.perf_counter()
    try:
        async with client.stream('GET', url, timeout=10,
                                 headers={'Range': f'bytes=0-{SERVER_MEASURE_BYTES - 1}'}) as response:
            if response.status_code >= 400:
                return {'ok': False, 'error': f'HTTP {response.status_code}'}
            async for chunk in response.aiter_bytes():
                received += len(chunk)
                if received >= SERVER_MEASURE_BYTES or time.perf_counter() - started > SERVER_MEASURE_BUDGET:
                    break
    except Exception as exc:
        return {'ok': False, 'error': type(exc).__name__, 'bytes': received}
    elapsed = max(1e-6, time.perf_counter() - started)
    return {'ok': received > 0, 'bytes': received, 'seconds': round(elapsed, 3),
            'mbps': round(received * 8 / elapsed / 1_000_000, 2)}


@app.post('/servers/measure')
async def servers_measure(payload: ServerMeasurePayload, kp_session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    """Availability + latency + throughput for every CDN region on offer.

    Each region is measured by actually switching to it and pulling the first
    few MB of a real stream, because that is the only thing that reflects what
    playback will do - the API host itself is the same for every region and
    tells you nothing about the CDN edge behind it.

    The switching is the awkward part and is handled honestly rather than
    hidden: the run is serialised behind a lock, and the previous selection is
    restored in a `finally` (or replaced by the winner when `apply_best`), so
    an exception mid-run cannot stand the account up on a region the user
    never chose. A stream URL already handed to a running player keeps working
    throughout - it is a full signed URL to a specific host, not a redirect
    resolved at play time - so this does not interrupt playback in progress.
    """
    session = await refresh_if_needed(kp_session or '', session_get(kp_session))
    if server_measure_lock.locked():
        raise HTTPException(409, 'Проверка серверов уже идёт')
    async with server_measure_lock:
        device = await _device_record(session)
        settings = device.get('settings') if isinstance(device.get('settings'), dict) else {}
        device_id = device.get('id')
        options = _server_options(settings)
        original = _selected_server_id(settings)
        if not device_id or not options:
            raise HTTPException(502, 'KinoPub did not report selectable servers')
        # `type` is mandatory on this endpoint (omitting it is a 400, not an
        # empty list - found the hard way), and it must be `movie`: a serial's
        # payload carries `seasons[].episodes[]` instead of the `videos[]`
        # the probe reads, so a serial would look like "no stream to test".
        item_id = None
        probe_error = None
        try:
            listing = await kino_get(session, 'v1/items/popular', {'type': 'movie', 'perpage': 5})
            for entry in extract_catalog_items(listing):
                if entry.get('id') is not None:
                    item_id = int(entry['id'])
                    break
            if item_id is None:
                probe_error = 'KinoPub вернул пустой список фильмов'
        except HTTPException as exc:
            probe_error = f'HTTP {exc.status_code}'
        except Exception as exc:
            probe_error = type(exc).__name__
        results: List[Dict[str, Any]] = []
        # Declared here, not in the `finally` that fills them in: the summary
        # below reads both, and burying their only assignment in a cleanup
        # block makes that look accidental.
        best: Optional[int] = None
        final: Optional[int] = original
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(12, connect=8)) as probe:
                for option in options:
                    row: Dict[str, Any] = {'id': option['id'], 'name': option['name']}
                    try:
                        await kino_post(session, f'v1/device/{device_id}/settings',
                                        {'serverLocation': option['id']})
                        # KinoPub needs a beat before freshly issued URLs come
                        # back on the new region; without it the first probe
                        # can still measure the previous one.
                        await asyncio.sleep(1.0)
                        url = await _probe_stream_url(session, item_id)
                        if not url:
                            row.update({'available': False,
                                        'error': probe_error or 'Нет потока для проверки'})
                            results.append(row)
                            continue
                        host = httpx.URL(url).host
                        row['host'] = host
                        row['latency_ms'] = await _connect_latency_ms(host)
                        download = await _measure_download(probe, url)
                        row['mbps'] = download.get('mbps')
                        row['available'] = bool(download.get('ok')) and row['latency_ms'] is not None
                        if not download.get('ok'):
                            row['error'] = download.get('error') or 'Не удалось скачать пробный отрезок'
                    except HTTPException as exc:
                        row.update({'available': False, 'error': f'HTTP {exc.status_code}'})
                    except Exception as exc:
                        row.update({'available': False, 'error': type(exc).__name__})
                    results.append(row)
        finally:
            # Throughput is the headline number - it is what decides whether a
            # stream buffers - but a 3 MB sample is genuinely noisy: measured
            # twice in a row, the same region came back 21.6 then 29.2 Mbps.
            # So a win only counts as a win with a >15% margin; inside that
            # band the two are called a tie and the far steadier latency
            # (best of three handshakes) breaks it. Without this the button
            # would recommend a different server on every press.
            usable = [row for row in results if row.get('available') and row.get('mbps')]
            best = None
            if usable:
                ranked = sorted(usable, key=lambda row: row['mbps'], reverse=True)
                best_row = ranked[0]
                contenders = [row for row in ranked if row['mbps'] >= best_row['mbps'] * 0.85]
                with_latency = [row for row in contenders if row.get('latency_ms') is not None]
                if len(contenders) > 1 and with_latency:
                    best_row = min(with_latency, key=lambda row: row['latency_ms'])
                best = best_row['id']
            final = best if (payload.apply_best and best is not None) else original
            if final is not None:
                with suppress(Exception):
                    await kino_post(session, f'v1/device/{device_id}/settings', {'serverLocation': final})
        for row in results:
            row['selected'] = row['id'] == final
        outcome = {
            'measured_at': int(time.time()),
            'selected': final,
            'previous': original,
            'applied_best': bool(payload.apply_best and best is not None),
            'results': results,
        }
        server_measure_last.clear()
        server_measure_last.update(outcome)
        log_event('device', 'CDN servers measured', outcome)
        return outcome


@app.get('/auth/status')
# Dict[str, Any], not Dict[str, bool]: FastAPI validates the response against
# this annotation, and `expires_in` is a number. A bool-only model made every
# authenticated call return 500, so reloading the page looked like a lost login.
async def auth_status(kp_session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    try:
        row = session_get(kp_session)
        if kp_session:
            asyncio.create_task(_ensure_device_registered(kp_session, row))
        return {
            'authenticated': True,
            'credentials_configured': bool(CLIENT_ID and CLIENT_SECRET),
            'has_refresh_token': bool(row.get('refresh_token')),
            'expires_in': max(0, int(float(row.get('expires_at') or 0) - time.time())),
        }
    except HTTPException:
        return {'authenticated': False, 'credentials_configured': bool(CLIENT_ID and CLIENT_SECRET)}


@app.post('/auth/device/start')
async def auth_device_start() -> Dict[str, Any]:
    require_credentials()
    params = {'grant_type': 'device_code', 'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET}
    response = await app.state.http.post(f'{API_BASE}/oauth2/device', params=params)
    if response.status_code >= 400:
        raise HTTPException(response.status_code, response.text)
    data = response.json()
    pending_devices[data['code']] = {'created_at': time.time(), **data}
    log_event('auth', 'Device authorization started')
    return {'code': data['code'], 'user_code': data['user_code'], 'verification_uri': data.get('verification_uri', 'https://kino.pub/device'), 'expires_in': data.get('expires_in', 8600), 'interval': data.get('interval', 5)}


@app.post('/auth/device/poll')
async def auth_device_poll(payload: DevicePoll, response: Response) -> Response:
    require_credentials()
    item = pending_devices.get(payload.code)
    if not item or item['created_at'] + int(item.get('expires_in', 8600)) < time.time():
        pending_devices.pop(payload.code, None)
        raise HTTPException(404, 'Unknown or expired device code')
    params = {'grant_type': 'device_token', 'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET, 'code': payload.code}
    upstream = await app.state.http.post(f'{API_BASE}/oauth2/device', params=params)
    data = upstream.json()
    if upstream.status_code == 400 and data.get('error') in {'authorization_pending', 'slow_down'}:
        return JSONResponse({'status': 'pending'}, status_code=202)
    if upstream.status_code >= 400:
        raise HTTPException(upstream.status_code, data)
    sid = secrets.token_urlsafe(32)
    new_session = {'access_token': data['access_token'], 'refresh_token': data.get('refresh_token'), 'expires_at': time.time() + int(data.get('expires_in', 3600)) - 60}
    session_save(sid, new_session)
    asyncio.create_task(_ensure_device_registered(sid, new_session))
    pending_devices.pop(payload.code, None)
    if not data.get('refresh_token'):
        log_event('auth', 'Device authorized WITHOUT a refresh token: the session will end when the access token expires', {
            'keys': sorted(str(k) for k in data.keys()),
        })
    else:
        log_event('auth', 'Device authorized')
    previous_task = getattr(app.state, 'page_count_task', None)
    if previous_task and not previous_task.done():
        previous_task.cancel()
    app.state.page_count_task = asyncio.create_task(prewarm_page_counts(force=True, sid=sid))
    # Set the cookie on the response that is actually returned. Copying headers
    # off the injected Response also copied its `content-length: 0`, and
    # Starlette leaves a caller-supplied length alone — so the reply advertised
    # zero bytes while carrying a body. The client read nothing, JSON parsing
    # failed, and the "authorized" branch never ran even though the cookie had
    # been set: the pairing screen stayed up and every retry created another
    # session server-side.
    result = JSONResponse({'status': 'authorized'})
    result.set_cookie('kp_session', sid, httponly=True, secure=COOKIE_SECURE, samesite='lax', max_age=60 * 60 * 24 * 30, path='/')
    return result


@app.post('/auth/logout')
def logout(response: Response, kp_session: Optional[str] = Cookie(default=None)) -> Dict[str, str]:
    if kp_session:
        with db_connect() as conn:
            conn.execute('DELETE FROM sessions WHERE sid = ?', (kp_session,))
    response.delete_cookie('kp_session', path='/')
    return {'status': 'ok'}



@app.get('/explorer')
async def api_explorer(path: str, query: str = '', download: bool = False, kp_session: Optional[str] = Cookie(default=None)):
    """Read-only authenticated API explorer. Tokens and sensitive headers are redacted."""
    clean_path = safe_explorer_path(path)
    session = await refresh_if_needed(kp_session or '', session_get(kp_session))
    params = {}
    if query.strip():
        try:
            params = dict(parse_qsl(query.lstrip('?'), keep_blank_values=True, strict_parsing=False))
        except ValueError as exc:
            raise HTTPException(400, 'Invalid query string') from exc
    headers = {'Authorization': f"Bearer {session['access_token']}", 'Accept': 'application/json, text/plain;q=0.9, */*;q=0.5'}
    try:
        upstream = await app.state.http.get(f'{API_BASE}/{clean_path}', params=params, headers=headers)
    except httpx.TimeoutException as exc:
        raise HTTPException(504, 'KinoPub API timeout') from exc
    except httpx.RequestError as exc:
        raise HTTPException(502, f'Could not connect to KinoPub API: {exc}') from exc
    raw = upstream.content
    if len(raw) > 2 * 1024 * 1024:
        raise HTTPException(413, 'Explorer response is larger than 2 MB')
    content_type = upstream.headers.get('content-type', '')
    try:
        payload = upstream.json()
        body = redact_value(payload)
        body_type = 'json'
    except ValueError:
        text = raw.decode('utf-8', errors='replace')
        body = text[:200000]
        body_type = 'text'
    response_headers = {
        k: redact_value(v, k) for k, v in upstream.headers.items()
        if k.lower() in {'content-type', 'content-length', 'cache-control', 'etag', 'last-modified', 'x-ratelimit-limit', 'x-ratelimit-remaining', 'retry-after'}
    }
    result = {
        'request': {'method': 'GET', 'path': '/' + clean_path, 'query': params},
        'response': {'status': upstream.status_code, 'content_type': content_type, 'headers': response_headers, 'body_type': body_type, 'body': body},
    }
    log_event('explorer', f'GET /{clean_path}', {'status': upstream.status_code})
    if download:
        filename = clean_path.replace('/', '_') or 'response'
        return JSONResponse(result, headers={'Content-Disposition': f'attachment; filename=kinopub_{filename}.json'})
    return result

@app.api_route('/api/{path:path}', methods=['GET', 'POST', 'PUT', 'DELETE'])
async def api_proxy(path: str, request: Request, kp_session: Optional[str] = Cookie(default=None)):
    session = await refresh_if_needed(kp_session or '', session_get(kp_session))
    body = await request.body()
    headers = {'Authorization': f"Bearer {session['access_token']}"}
    if request.headers.get('content-type'):
        headers['Content-Type'] = request.headers['content-type']
    proxy_params = dict(request.query_params)
    proxy_params['access_token'] = session['access_token']
    upstream = await app.state.http.request(request.method, f'{API_BASE}/{path}', params=proxy_params, content=body or None, headers=headers)
    excluded = {'content-encoding', 'transfer-encoding', 'connection'}
    out_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in excluded}
    log_event('api', f'{request.method} /{path}', {'status': upstream.status_code})
    return Response(upstream.content, status_code=upstream.status_code, headers=out_headers)


async def validate_stream_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(400, 'Invalid stream URL')
    hostname = parsed.hostname.lower().rstrip('.')
    if STREAM_HOST_SUFFIXES and not any(hostname == suffix or hostname.endswith('.' + suffix) for suffix in STREAM_HOST_SUFFIXES):
        raise HTTPException(403, 'Stream host is not allowed')
    try:
        infos = await asyncio.get_running_loop().run_in_executor(None, socket.getaddrinfo, hostname, parsed.port or (443 if parsed.scheme == 'https' else 80), 0, socket.SOCK_STREAM)
    except socket.gaierror:
        raise HTTPException(400, 'Stream host cannot be resolved')
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise HTTPException(403, 'Private or unsafe stream address is blocked')
    return url


def media_headers(session: Dict[str, Any], request: Request) -> Dict[str, str]:
    headers = {'Authorization': f"Bearer {session['access_token']}", 'User-Agent': request.headers.get('user-agent', 'Mozilla/5.0')}
    if request.headers.get('range'):
        headers['Range'] = request.headers['range']
    if MEDIA_REFERER:
        headers['Referer'] = MEDIA_REFERER
    if MEDIA_ORIGIN:
        headers['Origin'] = MEDIA_ORIGIN
    return headers


async def open_media(url: str, headers: Dict[str, str], max_redirects: int = 3):
    current = await validate_stream_url(url)
    for _ in range(max_redirects + 1):
        req = app.state.http.build_request('GET', current, headers=headers)
        upstream = await app.state.http.send(req, stream=True)
        if upstream.status_code not in {301, 302, 303, 307, 308}:
            return upstream, current
        location = upstream.headers.get('location')
        await upstream.aclose()
        if not location:
            raise HTTPException(502, 'Media redirect has no location')
        current = await validate_stream_url(urljoin(current, location))
    raise HTTPException(502, 'Too many media redirects')


def relay_url(url: str, hls: bool = False) -> str:
    endpoint = '/bridge/hls' if hls else '/bridge/stream'
    return endpoint + '?url=' + quote(url, safe='')


def rewrite_hls(text: str, playlist_url: str) -> str:
    output = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            output.append(raw)
            continue
        if line.startswith('#'):
            if 'URI="' in raw:
                before, rest = raw.split('URI="', 1)
                uri, after = rest.split('"', 1)
                absolute = urljoin(playlist_url, uri)
                output.append(before + 'URI="' + relay_url(absolute, absolute.lower().split('?', 1)[0].endswith('.m3u8')) + '"' + after)
            else:
                output.append(raw)
            continue
        absolute = urljoin(playlist_url, line)
        is_playlist = absolute.lower().split('?', 1)[0].endswith('.m3u8')
        output.append(relay_url(absolute, is_playlist))
    return '\n'.join(output) + '\n'



# Decoded-and-re-encoded posters, keyed by exactly what decides the bytes.
# Every grid render asked for ~48 of these and each one was a fresh fetch from
# staticpop plus a full LANCZOS resize and progressive JPEG encode - ~70ms of
# work per poster, repeated on every page load. The results are immutable
# (the source URLs are content-addressed), so they are worth holding on to.
# Bounded by total bytes rather than entry count: a backdrop is ~20x a poster.
IMAGE_CACHE_MAX_BYTES = 96 * 1024 * 1024
image_cache: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
image_cache_bytes = 0


def _image_cache_get(key: str) -> Optional[Dict[str, Any]]:
    entry = image_cache.get(key)
    if entry is not None:
        image_cache.move_to_end(key)
    return entry


def _image_cache_put(key: str, content: bytes, media_type: str, etag: str) -> None:
    global image_cache_bytes
    if len(content) > IMAGE_CACHE_MAX_BYTES // 4:
        return
    existing = image_cache.pop(key, None)
    if existing:
        image_cache_bytes -= len(existing['content'])
    image_cache[key] = {'content': content, 'media_type': media_type, 'etag': etag}
    image_cache_bytes += len(content)
    while image_cache_bytes > IMAGE_CACHE_MAX_BYTES and image_cache:
        _, evicted = image_cache.popitem(last=False)
        image_cache_bytes -= len(evicted['content'])


@app.get('/image')
async def image_proxy(
    url: str,
    request: Request,
    width: int = 0,
    height: int = 0,
    quality: int = 82,
    fallback: str = '',
):
    """Same-origin image proxy with optional resizing for TV browsers.

    Poster requests use a small fixed size, while detail backdrops use a larger
    size. JPEG output is broadly compatible with older webOS browsers and is
    substantially smaller than the original source images.

    ``fallback`` is fetched when the primary URL is missing. The CDN only has a
    wide 16:9 backdrop for some items, and a CSS background cannot retry a 404,
    so the substitution happens here and the client stays unaware.
    """
    width = max(0, min(int(width or 0), 1920))
    height = max(0, min(int(height or 0), 1080))
    quality = max(55, min(int(quality or 82), 92))
    # The `v=` cache-buster the frontend appends is deliberately not part of
    # this key: it exists to make *browsers* refetch, and honouring it here
    # would mean re-downloading and re-encoding an identical image.
    cache_key = f'{url}|{fallback}|{width}|{height}|{quality}'
    cached = _image_cache_get(cache_key)
    if cached is not None:
        if request.headers.get('if-none-match') == cached['etag']:
            return Response(status_code=304, headers={
                'ETag': cached['etag'], 'Cache-Control': 'public, max-age=2592000, immutable'})
        return Response(cached['content'], media_type=cached['media_type'], headers={
            'ETag': cached['etag'], 'Cache-Control': 'public, max-age=2592000, immutable'})
    headers = {
        'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
        'User-Agent': request.headers.get('user-agent', 'Mozilla/5.0'),
    }
    if IMAGE_REFERER:
        headers['Referer'] = IMAGE_REFERER

    async def fetch(target: str):
        safe = await validate_stream_url(target)
        try:
            return await app.state.http.get(safe, headers=headers, follow_redirects=True)
        except httpx.TimeoutException as exc:
            raise HTTPException(504, 'Image request timed out') from exc
        except httpx.RequestError as exc:
            raise HTTPException(502, f'Could not load image: {exc}') from exc

    safe_url = url
    upstream = await fetch(url)
    if upstream.status_code >= 400 and fallback:
        log_event('image', 'Primary image missing, using fallback', {
            'status': upstream.status_code, 'host': urlparse(url).hostname,
        })
        safe_url = fallback
        upstream = await fetch(fallback)
    if upstream.status_code >= 400:
        raise HTTPException(upstream.status_code, 'Upstream image request failed')
    content_type = upstream.headers.get('content-type', '').split(';', 1)[0].strip().lower()
    if not content_type.startswith('image/'):
        raise HTTPException(415, 'Upstream URL is not an image')
    content = upstream.content
    if len(content) > IMAGE_MAX_BYTES:
        raise HTTPException(413, 'Image is too large')

    output_type = content_type
    if width or height:
        try:
            with Image.open(BytesIO(content)) as source:
                source = ImageOps.exif_transpose(source)
                target_w = width or source.width
                target_h = height or source.height
                source.thumbnail((target_w, target_h), Image.Resampling.LANCZOS)
                if source.mode not in ('RGB', 'L'):
                    background = Image.new('RGB', source.size, (20, 24, 34))
                    if 'A' in source.mode:
                        background.paste(source, mask=source.getchannel('A'))
                    else:
                        background.paste(source.convert('RGB'))
                    source = background
                elif source.mode != 'RGB':
                    source = source.convert('RGB')
                buffer = BytesIO()
                source.save(buffer, format='JPEG', quality=quality, optimize=True, progressive=True)
                content = buffer.getvalue()
                output_type = 'image/jpeg'
        except Exception as exc:
            log_event('image', 'Image optimization skipped', {'error': str(exc)[:180]})

    etag = '"' + hashlib.sha1(content).hexdigest() + '"'
    _image_cache_put(cache_key, content, output_type, etag)
    response_headers = {
        'Cache-Control': 'public, max-age=2592000, immutable',
        'ETag': etag,
    }
    log_event('image', 'Image proxied', {
        'host': urlparse(safe_url).hostname,
        'bytes': len(content),
        'width': width,
        'height': height,
        'quality': quality,
    })
    if request.headers.get('if-none-match') == etag:
        return Response(status_code=304, headers=response_headers)
    return Response(content, media_type=output_type, headers=response_headers)



def _vtt_seconds(value: str) -> Optional[float]:
    clean = value.strip().replace(',', '.')
    parts = clean.split(':')
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
    except (TypeError, ValueError):
        return None
    return None


def _vtt_timestamp(seconds: float) -> str:
    millis = max(0, int(round(seconds * 1000)))
    hours, millis = divmod(millis, 3600000)
    minutes, millis = divmod(millis, 60000)
    secs, millis = divmod(millis, 1000)
    return f'{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}'


def _shift_vtt(text: str, offset: float) -> str:
    """Move every cue earlier by ``offset`` seconds; a negative value delays them.

    KinoPub ships a per-track ``shift`` and local HLS can start at a non-zero
    point, so both directions are needed.
    """
    if not offset:
        return text
    blocks = text.replace('\r\n', '\n').replace('\r', '\n').split('\n\n')
    shifted = []
    for block in blocks:
        lines = block.split('\n')
        timing_index = next((i for i, line in enumerate(lines) if '-->' in line), -1)
        if timing_index < 0:
            shifted.append(block)
            continue
        left, right = lines[timing_index].split('-->', 1)
        right_parts = right.strip().split(None, 1)
        start = _vtt_seconds(left)
        end = _vtt_seconds(right_parts[0]) if right_parts else None
        if start is None or end is None:
            shifted.append(block)
            continue
        end -= offset
        if end <= 0:
            continue
        start = max(0.0, start - offset)
        settings = (' ' + right_parts[1]) if len(right_parts) > 1 else ''
        lines[timing_index] = f'{_vtt_timestamp(start)} --> {_vtt_timestamp(end)}{settings}'
        shifted.append('\n'.join(lines))
    return '\n\n'.join(shifted).rstrip() + '\n'


def subtitle_to_vtt(text: str, offset: float = 0) -> str:
    text = text.replace('\r\n', '\n').replace('\r', '\n').lstrip('\ufeff')
    if text.lstrip().startswith('WEBVTT'):
        return _shift_vtt(text, offset)
    lines = text.split('\n')
    out = ['WEBVTT', '']
    for line in lines:
        stripped = line.strip()
        if '-->' in line:
            line = line.replace(',', '.')
        if stripped.isdigit():
            continue
        if stripped.startswith('Dialogue:'):
            parts = line.split(',', 9)
            if len(parts) >= 10:
                start, end, body = parts[1], parts[2], parts[9]
                def ass_time(value: str) -> str:
                    bits = value.strip().split(':')
                    if len(bits) == 3:
                        h, m, sec = bits
                        if '.' not in sec:
                            sec += '.000'
                        elif len(sec.split('.', 1)[1]) == 2:
                            sec += '0'
                        return f'{int(h):02d}:{int(m):02d}:{sec}'
                    return value
                out.append(f'{ass_time(start)} --> {ass_time(end)}')
                out.append(body.replace('\\N', '\n').replace('{\\i1}', '<i>').replace('{\\i0}', '</i>'))
                out.append('')
            continue
        out.append(line)
    return _shift_vtt('\n'.join(out), offset)

@app.get('/subtitle')
async def subtitle(url: str, request: Request, offset: float = 0, kp_session: Optional[str] = Cookie(default=None)):
    session = await refresh_if_needed(kp_session or '', session_get(kp_session))
    upstream, final_url = await open_media(url, media_headers(session, request))
    try:
        if upstream.status_code >= 400:
            raise HTTPException(upstream.status_code, 'Upstream subtitle request failed')
        content = await upstream.aread()
        encoding = upstream.encoding or 'utf-8'
        try:
            text = content.decode(encoding, errors='replace')
        except LookupError:
            text = content.decode('utf-8', errors='replace')
    finally:
        await upstream.aclose()
    log_event('media', 'Subtitle relayed', {'url': final_url, 'bytes': len(content)})
    shift = max(-3600.0, min(3600.0, float(offset or 0)))
    return PlainTextResponse(subtitle_to_vtt(text, shift), media_type='text/vtt; charset=utf-8', headers={'Cache-Control': 'no-store'})

def _hls_attributes(raw: str) -> Dict[str, str]:
    attributes: Dict[str, str] = {}
    for match in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', raw):
        attributes[match.group(1)] = match.group(2).strip('"')
    return attributes


def _hls_audio_renditions(text: str) -> List[Dict[str, Any]]:
    """Alternate audio renditions declared by an HLS master playlist."""
    renditions: List[Dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith('#EXT-X-MEDIA:'):
            continue
        attributes = _hls_attributes(line[len('#EXT-X-MEDIA:'):])
        if str(attributes.get('TYPE', '')).upper() != 'AUDIO':
            continue
        renditions.append({
            'name': attributes.get('NAME', ''),
            'language': attributes.get('LANGUAGE', ''),
            'group_id': attributes.get('GROUP-ID', ''),
            'channels': attributes.get('CHANNELS', ''),
            'default': str(attributes.get('DEFAULT', 'NO')).upper() == 'YES',
        })
    return renditions


@app.get('/media/audio-variants')
async def media_audio_variants(url: str, request: Request, kp_session: Optional[str] = Cookie(default=None)):
    """Report the alternate audio renditions a KinoPub HLS variant exposes.

    When a variant declares two or more, the player can switch audio through
    hls.js alone: no remux, no restart, and seeking keeps working normally.
    """
    session = await refresh_if_needed(kp_session or '', session_get(kp_session))
    upstream, final_url = await open_media(url, media_headers(session, request))
    try:
        if upstream.status_code >= 400:
            raise HTTPException(upstream.status_code, 'Upstream playlist request failed')
        text = (await upstream.aread()).decode('utf-8-sig', errors='replace')
    finally:
        await upstream.aclose()
    renditions = _hls_audio_renditions(text)
    log_event('media', 'HLS audio renditions probed', {
        'host': urlparse(final_url).hostname,
        'master': '#EXT-X-STREAM-INF' in text,
        'count': len(renditions),
        'names': [x['name'] or x['language'] for x in renditions][:8],
    })
    return {
        'url': url,
        'master': '#EXT-X-STREAM-INF' in text,
        'count': len(renditions),
        'renditions': renditions,
    }


@app.get('/hls')
async def hls(url: str, request: Request, kp_session: Optional[str] = Cookie(default=None)):
    session = await refresh_if_needed(kp_session or '', session_get(kp_session))
    upstream, final_url = await open_media(url, media_headers(session, request))
    try:
        if upstream.status_code >= 400:
            raise HTTPException(upstream.status_code, 'Upstream playlist request failed')
        content = await upstream.aread()
        text = content.decode('utf-8-sig', errors='replace')
    finally:
        await upstream.aclose()
    log_event('media', 'HLS playlist relayed', {'url': final_url, 'bytes': len(content)})
    return PlainTextResponse(rewrite_hls(text, final_url), media_type='application/vnd.apple.mpegurl', headers={'Cache-Control': 'no-store'})




def _ffmpeg_headers(headers: Dict[str, str]) -> str:
    return ''.join(f'{key}: {value}\r\n' for key, value in headers.items() if value and key.lower() != 'range')


# Present in every FFmpeg release this project can plausibly run on.
_FFMPEG_BASE_HTTP_OPTIONS = [
    '-seekable', '1',
    '-rw_timeout', '30000000',
    '-reconnect', '1',
    '-reconnect_streamed', '1',
    '-reconnect_delay_max', '4',
]
# Added in later releases. Passing one FFmpeg does not know is fatal
# ("Option not found"), which would kill every job before it starts, so each is
# used only after the installed binary confirms it.
_FFMPEG_OPTIONAL_HTTP_OPTIONS = [
    ('multiple_requests', ['-multiple_requests', '1']),
    ('reconnect_on_network_error', ['-reconnect_on_network_error', '1']),
    ('reconnect_on_http_error', ['-reconnect_on_http_error', '429,500,502,503,504']),
    ('reconnect_max_retries', ['-reconnect_max_retries', '30']),
    ('reconnect_delay_total_max', ['-reconnect_delay_total_max', '180']),
    ('respect_retry_after', ['-respect_retry_after', '1']),
]
_ffmpeg_http_options_cache: Optional[List[str]] = None


async def _ffmpeg_http_reconnect_options() -> List[str]:
    """Make long KinoPub CDN reads survive premature TLS/HTTP disconnects.

    The option set is resolved once against the installed FFmpeg and reused.
    """
    global _ffmpeg_http_options_cache
    if _ffmpeg_http_options_cache is not None:
        return list(_ffmpeg_http_options_cache)
    help_text = ''
    try:
        process = await asyncio.create_subprocess_exec(
            'ffmpeg', '-hide_banner', '-h', 'protocol=http',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=15)
        help_text = stdout.decode('utf-8', errors='replace')
    except (FileNotFoundError, OSError, asyncio.TimeoutError) as exc:
        log_event('media', 'FFmpeg HTTP option probe failed', {'error': str(exc)[:200]})
    options = list(_FFMPEG_BASE_HTTP_OPTIONS)
    supported = []
    for name, flags in _FFMPEG_OPTIONAL_HTTP_OPTIONS:
        if help_text and re.search(r'^\s*-' + re.escape(name) + r'\b', help_text, re.M):
            options += flags
            supported.append(name)
    _ffmpeg_http_options_cache = options
    log_event('media', 'FFmpeg HTTP options resolved', {'optional': supported, 'probed': bool(help_text)})
    return list(options)


def _playlist_metrics(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {'segments': 0, 'duration': 0.0, 'ended': False}
    try:
        text = path.read_text('utf-8', errors='replace')
    except OSError:
        return {'segments': 0, 'duration': 0.0, 'ended': False}
    duration = 0.0
    segments = 0
    for line in text.splitlines():
        if line.startswith('#EXTINF:'):
            try:
                duration += float(line.split(':', 1)[1].split(',', 1)[0])
                segments += 1
            except (ValueError, IndexError):
                pass
    return {'segments': segments, 'duration': round(duration, 3), 'ended': '#EXT-X-ENDLIST' in text}


async def _inspect_audio_source(url: str, headers: Dict[str, str], ordinal: int) -> Dict[str, Any]:
    """Validate the requested audio track position against the real file.

    ``ordinal`` is the zero-based position inside ``media.audios``, which is the
    same ordering FFmpeg uses for ``0:a:N``. KinoPub's ``audios[].index`` is a
    per-file track number, not an absolute stream index, so it must never be fed
    to ``-map 0:<n>`` directly: a cover-art stream or an embedded subtitle track
    shifts absolute indexes and silently selects the wrong audio.
    """
    command = ['ffprobe', '-v', 'error', *(await _ffmpeg_http_reconnect_options())]
    raw_headers = _ffmpeg_headers(headers)
    if raw_headers:
        command += ['-headers', raw_headers]
    command += [
        '-show_entries',
        'format=duration:stream=index,codec_type,codec_name,channels,channel_layout',
        '-of', 'json', url,
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise HTTPException(500, 'FFmpeg is not installed') from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise HTTPException(504, 'Timed out while inspecting the media file') from exc
    if process.returncode != 0:
        detail = stderr.decode('utf-8', errors='replace')[-1000:]
        log_event('media', 'Audio HLS probe failed', {'ordinal': ordinal, 'error': detail})
        raise HTTPException(502, 'Could not inspect audio tracks in the HTTP source')
    try:
        payload = json.loads(stdout.decode('utf-8', errors='replace') or '{}')
    except json.JSONDecodeError as exc:
        raise HTTPException(502, 'Invalid FFprobe response') from exc
    streams = payload.get('streams') if isinstance(payload, dict) else []
    audio_streams = [stream for stream in streams or [] if stream.get('codec_type') == 'audio']
    if not audio_streams:
        raise HTTPException(409, 'В этом файле нет звуковых дорожек')
    if not 0 <= ordinal < len(audio_streams):
        log_event('media', 'Audio HLS track missing', {
            'ordinal': ordinal,
            'available': len(audio_streams),
            'absolute': [int(stream.get('index', -1)) for stream in audio_streams],
        })
        raise HTTPException(409, f'В этом качестве только {len(audio_streams)} звуковых дорожек')
    selected = audio_streams[ordinal]
    try:
        duration = max(0.0, float((payload.get('format') or {}).get('duration') or 0))
    except (TypeError, ValueError):
        duration = 0.0
    return {'stream': selected, 'duration': duration, 'ordinal': ordinal, 'audio_count': len(audio_streams)}


def _audio_job_public(job: Dict[str, Any]) -> Dict[str, Any]:
    metrics = _playlist_metrics(job['playlist'])
    status = job.get('status', 'starting')
    process = job.get('process')
    if status not in {'failed', 'complete', 'stopped'} and process and process.returncode is not None:
        if process.returncode == 0 and metrics['segments']:
            status = 'complete'
        elif process.returncode != 0:
            status = 'failed'
    if status == 'starting' and metrics['segments'] >= 2 and metrics['duration'] >= AUDIO_HLS_SEGMENT_SECONDS:
        status = 'ready'
    if status == 'ready' and metrics['ended']:
        status = 'complete'
    job['status'] = status
    result = {
        'job_id': job['id'],
        'status': status,
        'start_offset': job['start'],
        'available_duration': metrics['duration'],
        'segments': metrics['segments'],
        'original_duration': job.get('duration', 0),
    }
    if status in {'ready', 'complete'}:
        result['playlist_url'] = f"/bridge/audio-hls/{job['id']}/index.m3u8"
    if status == 'failed':
        result['error'] = job.get('error') or 'Не удалось подготовить выбранную звуковую дорожку.'
    if status == 'stopped':
        result['error'] = 'Подготовка дорожки остановлена.'
    return result


async def _monitor_audio_hls_job(job_id: str) -> None:
    job = audio_hls_jobs.get(job_id)
    if not job:
        return
    process = job['process']
    code = await process.wait()
    if job.get('status') == 'stopped':
        return
    metrics = _playlist_metrics(job['playlist'])
    if code == 0 and metrics['segments']:
        job['status'] = 'complete'
        log_event('media', 'Audio HLS completed', {
            'job': job_id,
            'track': job['track'],
            'segments': metrics['segments'],
            'duration': metrics['duration'],
        })
        return
    error = ''
    try:
        error = job['stderr'].read_text('utf-8', errors='replace')[-1600:]
    except OSError:
        pass
    job['status'] = 'failed'
    job['error'] = 'FFmpeg не смог подготовить HLS с выбранной дорожкой.'
    # Release the dedup key so the next attempt starts a fresh process instead
    # of being handed this corpse until the TTL expires.
    key = job.get('key')
    if key and audio_hls_job_keys.get(key) == job_id:
        audio_hls_job_keys.pop(key, None)
    log_event('media', 'Audio HLS failed', {'job': job_id, 'track': job['track'], 'code': code, 'error': error})


async def audio_hls_cleanup_loop() -> None:
    while True:
        await asyncio.sleep(60)
        cutoff = time.time() - AUDIO_HLS_TTL
        for job_id, job in list(audio_hls_jobs.items()):
            remove_after = job.get('remove_after')
            if remove_after and time.time() < remove_after:
                continue
            if not remove_after and job.get('touched', job.get('created', 0)) >= cutoff:
                continue
            process = job.get('process')
            if process and process.returncode is None:
                process.terminate()
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=2)
                if process.returncode is None:
                    process.kill()
            audio_hls_jobs.pop(job_id, None)
            key = job.get('key')
            if key and audio_hls_job_keys.get(key) == job_id:
                audio_hls_job_keys.pop(key, None)
            shutil.rmtree(job['dir'], ignore_errors=True)


@app.post('/audio-hls/jobs')
async def create_audio_hls_job(payload: AudioHlsPayload, request: Request, kp_session: Optional[str] = Cookie(default=None)):
    # The only endpoint in this bridge that genuinely needs FFmpeg. Refused up
    # front with a message the player can show as-is, rather than letting the
    # subprocess fail somewhere inside a background job.
    if not FFMPEG_AVAILABLE:
        raise HTTPException(503, 'Сборка без FFmpeg: пересобрать дорожку на сервере нельзя. '
                                 'Доступны только дорожки, которые есть в самом потоке.')
    if payload.track < 0 or payload.track > 128:
        raise HTTPException(400, 'Invalid audio track index')
    sid = kp_session or ''
    session = await refresh_if_needed(sid, session_get(kp_session))
    safe_url = await validate_stream_url(payload.url)
    headers = media_headers(session, request)
    # Resolve and validate redirects with the same SSRF rules used by the normal relay.
    upstream, final_url = await open_media(safe_url, headers)
    try:
        if upstream.status_code >= 400:
            raise HTTPException(upstream.status_code, 'Upstream media request failed')
    finally:
        await upstream.aclose()
    inspected = await _inspect_audio_source(final_url, headers, payload.track)
    selected = inspected['stream']
    ordinal = int(inspected['ordinal'])
    requested_start = max(0.0, float(payload.start or 0))
    bucket_start = float(int(requested_start // AUDIO_HLS_START_BUCKET) * AUDIO_HLS_START_BUCKET)
    source_key = hashlib.sha256(final_url.encode('utf-8')).hexdigest()
    session_key = hashlib.sha256(sid.encode('utf-8')).hexdigest()[:16]
    job_key = f'{session_key}:{source_key}:a{ordinal}:{bucket_start:.3f}'
    existing_id = audio_hls_job_keys.get(job_key)
    existing = audio_hls_jobs.get(existing_id or '')
    if existing:
        # Recompute before reusing: a job whose FFmpeg already died still holds
        # status 'starting', and reusing it would fail every retry until the TTL.
        snapshot = _audio_job_public(existing)
        if snapshot['status'] not in {'failed', 'stopped'}:
            existing['touched'] = time.time()
            return snapshot
        audio_hls_job_keys.pop(job_key, None)

    job_id = secrets.token_urlsafe(12).replace('-', '').replace('_', '')
    job_dir = AUDIO_HLS_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=False)
    playlist = job_dir / 'index.m3u8'
    stderr_path = job_dir / 'ffmpeg.log'
    segment_pattern = str(job_dir / 'segment_%06d.ts')
    raw_headers = _ffmpeg_headers(headers)
    command = ['ffmpeg', '-hide_banner', '-loglevel', 'warning', '-nostdin']
    command += await _ffmpeg_http_reconnect_options()
    if bucket_start > 0:
        command += ['-ss', f'{bucket_start:.3f}']
    if raw_headers:
        command += ['-headers', raw_headers]
    command += [
        # No '+discardcorrupt': on a reconnecting CDN read it silently drops
        # packets, which is exactly how the audio disappears mid-playback.
        '-fflags', '+genpts',
        '-i', final_url,
        '-map', '0:v:0',
        # Position-based selection. '0:<absolute index>' picks the wrong stream
        # whenever the MP4 carries cover art or embedded subtitles.
        '-map', f'0:a:{ordinal}',
        '-sn', '-dn',
        '-c:v', 'copy',
        # No explicit '-bsf:v h264_mp4toannexb': the HLS muxer applies the right
        # bitstream filter itself, and forcing a second pass over already
        # converted NALs is what produces "Invalid NAL unit size". It also
        # breaks outright on HEVC sources.
        '-c:a', 'aac',
        '-profile:a', 'aac_low',
        '-ar', '48000',
        '-b:a', '192k',
        '-ac', '2',
        # 'first_pts=0' pinned audio to zero while video kept the seek-adjusted
        # timeline, drifting A/V apart on every non-zero start.
        '-af', 'aresample=async=1',
        '-max_muxing_queue_size', '4096',
        '-avoid_negative_ts', 'make_zero',
        '-f', 'hls',
        '-hls_time', str(AUDIO_HLS_SEGMENT_SECONDS),
        '-hls_list_size', '0',
        '-hls_playlist_type', 'event',
        '-hls_flags', 'independent_segments+temp_file',
        '-hls_segment_filename', segment_pattern,
        str(playlist),
    ]
    stderr_file = stderr_path.open('wb')
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=stderr_file,
        )
    except FileNotFoundError as exc:
        stderr_file.close()
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(500, 'FFmpeg is not installed') from exc
    finally:
        stderr_file.close()
    now = time.time()
    job = {
        'id': job_id,
        'key': job_key,
        'sid': sid,
        'dir': job_dir,
        'playlist': playlist,
        'stderr': stderr_path,
        'process': process,
        'status': 'starting',
        'created': now,
        'touched': now,
        'start': bucket_start,
        'requested_start': requested_start,
        'track': ordinal,
        'duration': inspected.get('duration', 0),
    }
    audio_hls_jobs[job_id] = job
    audio_hls_job_keys[job_key] = job_id
    asyncio.create_task(_monitor_audio_hls_job(job_id))
    log_event('media', 'Audio HLS started', {
        'job': job_id,
        'host': urlparse(final_url).hostname,
        'ordinal': ordinal,
        'audio_count': inspected.get('audio_count'),
        'absolute_index': selected.get('index'),
        'start': bucket_start,
        'codec': selected.get('codec_name'),
        'channels': selected.get('channels'),
    })
    return _audio_job_public(job)


@app.get('/audio-hls/jobs/{job_id}')
async def audio_hls_job_status(job_id: str, kp_session: Optional[str] = Cookie(default=None)):
    session_get(kp_session)
    job = audio_hls_jobs.get(job_id)
    if not job or job.get('sid') != (kp_session or ''):
        raise HTTPException(404, 'Audio HLS job not found')
    job['touched'] = time.time()
    return _audio_job_public(job)


@app.delete('/audio-hls/jobs/{job_id}')
async def stop_audio_hls_job(job_id: str, kp_session: Optional[str] = Cookie(default=None)):
    session_get(kp_session)
    job = audio_hls_jobs.get(job_id)
    if not job or job.get('sid') != (kp_session or ''):
        raise HTTPException(404, 'Audio HLS job not found')
    process = job.get('process')
    job['status'] = 'stopped'
    job['touched'] = time.time()
    job['remove_after'] = time.time() + 90
    if process and process.returncode is None:
        process.terminate()
    return {'job_id': job_id, 'status': 'stopped'}


@app.get('/audio-hls/{job_id}/{filename}')
async def audio_hls_file(job_id: str, filename: str, kp_session: Optional[str] = Cookie(default=None)):
    session_get(kp_session)
    job = audio_hls_jobs.get(job_id)
    if not job or job.get('sid') != (kp_session or ''):
        raise HTTPException(404, 'Audio HLS job not found')
    if Path(filename).name != filename or filename.startswith('.'):
        raise HTTPException(400, 'Invalid HLS file name')
    if filename != 'index.m3u8' and not filename.startswith('segment_'):
        raise HTTPException(404, 'HLS file not found')
    path = job['dir'] / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(404, 'HLS fragment is not ready yet')
    job['touched'] = time.time()
    if filename.endswith('.m3u8'):
        return FileResponse(path, media_type='application/vnd.apple.mpegurl', headers={'Cache-Control': 'no-store'})
    return FileResponse(path, media_type='video/mp2t', headers={'Cache-Control': 'private, max-age=21600'})


@app.get('/stream')
async def stream(url: str, request: Request, kp_session: Optional[str] = Cookie(default=None)):
    session = await refresh_if_needed(kp_session or '', session_get(kp_session))
    upstream, final_url = await open_media(url, media_headers(session, request))
    if upstream.status_code >= 400:
        await upstream.aclose()
        raise HTTPException(upstream.status_code, 'Upstream media request failed')
    allowed = {'content-type', 'content-length', 'content-range', 'accept-ranges', 'etag', 'last-modified', 'cache-control'}
    headers = {k: v for k, v in upstream.headers.items() if k.lower() in allowed}
    log_event('media', 'Stream opened', {'url': final_url, 'status': upstream.status_code, 'range': request.headers.get('range', '')})

    async def iterator():
        try:
            async for chunk in upstream.aiter_bytes(256 * 1024):
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(iterator(), status_code=upstream.status_code, headers=headers)


@app.post('/debug/events')
def receive_debug(event: DebugEvent) -> Dict[str, str]:
    log_event(event.kind, event.message, event.details)
    return {'status': 'ok'}


@app.get('/debug/events')
def get_debug_events() -> Dict[str, Any]:
    return {'events': list(reversed(debug_events))}


@app.get('/settings')
def get_settings() -> Dict[str, Any]:
    defaults=SettingsPayload().model_dump()
    with db_connect() as conn:
        row=conn.execute("SELECT payload FROM user_settings WHERE profile='default'").fetchone()
    if row:
        try: defaults.update(json.loads(row['payload']))
        except Exception: pass
    return defaults

@app.put('/settings')
def put_settings(payload: SettingsPayload) -> Dict[str, Any]:
    data=payload.model_dump()
    with db_connect() as conn:
        conn.execute("INSERT INTO user_settings(profile,payload,updated_at) VALUES('default',?,?) ON CONFLICT(profile) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at", (json.dumps(data,ensure_ascii=False),time.time()))
    return data

@app.get('/watching/statuses')
async def watching_statuses(kp_session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    """Return watched statuses known by KinoPub history.

    Catalogue payloads remain the primary source because they can expose the
    exact ``watched`` state. History is used to enrich cards that omit it.
    """
    session = await refresh_if_needed(kp_session or '', session_get(kp_session))
    statuses: Dict[str, int] = {}
    page = 1
    max_pages = 20
    while page <= max_pages:
        payload = await kino_get(session, 'v1/history', {'page': page, 'perpage': 50})
        history = payload.get('history') if isinstance(payload, dict) else None
        if not isinstance(history, list) or not history:
            break
        for entry in history:
            if not isinstance(entry, dict):
                continue
            item = entry.get('item') if isinstance(entry.get('item'), dict) else {}
            media = entry.get('media') if isinstance(entry.get('media'), dict) else {}
            item_id = str(item.get('id') or entry.get('item_id') or '').strip()
            if not item_id:
                continue
            status = _extract_watched_status(item)
            if status == -1:
                status = _extract_watched_status(media)
            if status == -1:
                status = 0  # Presence in history means playback at least started.
            statuses[item_id] = max(statuses.get(item_id, -1), status)
        pagination = payload.get('pagination') if isinstance(payload, dict) else {}
        try:
            total_pages = int((pagination or {}).get('total') or 0)
        except (TypeError, ValueError):
            total_pages = 0
        if len(history) < 50 or (total_pages and page >= total_pages):
            break
        page += 1
    return {'statuses': statuses, 'count': len(statuses)}


def _progress_rows_from_watching(media_id: str, payload: Any) -> List[Dict[str, Any]]:
    """`v1/watching` -> строки в том же виде, что раньше отдавала SQLite.

    Форма ответа сохранена ровно потому, что её читает фронтенд
    (`progressRows`/`episodeProgress`/`latestResumable`): `episode_id`,
    `position`, `duration`, `completed` и порядок «сначала недавнее».
    Фильмы приходят в `videos`, сериалы - в `seasons[].episodes`; и там и
    там есть `time`, `duration`, `status` и `updated`, поэтому обе ветки
    сводятся к одному списку.
    """
    item = payload.get('item') if isinstance(payload, dict) else None
    item = item if isinstance(item, dict) else {}
    entries: List[Dict[str, Any]] = []
    for video in item.get('videos') or []:
        if isinstance(video, dict):
            entries.append(video)
    for season in item.get('seasons') or []:
        if not isinstance(season, dict):
            continue
        for episode in season.get('episodes') or []:
            if isinstance(episode, dict):
                entries.append(episode)

    rows: List[Dict[str, Any]] = []
    for entry in entries:
        position = _plain_number(entry.get('time')) or 0
        duration = _plain_number(entry.get('duration')) or 0
        # `status` у KinoPub: 1 - досмотрено, 0 - в процессе, -1 - не начато.
        # Запись без позиции не нужна: локальная таблица такие тоже не
        # хранила, а «Продолжить» на нулевой секунде показывать нечего.
        status = _first_int(entry.get('status'))
        if position <= 0 and status != 1:
            continue
        rows.append({
            'media_id': str(media_id),
            'episode_id': str(entry.get('id') or ''),
            'position': float(position),
            'duration': float(duration),
            'completed': 1 if status == 1 else 0,
            'updated_at': float(_plain_number(entry.get('updated')) or 0),
        })
    rows.sort(key=lambda r: r['updated_at'], reverse=True)
    return rows


@app.get('/history')
async def get_history(media_id: str = '', kp_session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    """Позиции возобновления для одного тайтла, недавние первыми.

    Раньше здесь была своя таблица `watch_progress`, дублировавшая то, что
    KinoPub и так хранит: каждое сохранение прогресса уходит к нему через
    `v1/watching/marktime` (см. `_mirror_watch_progress`), а обратно всё
    возвращается в `v1/watching`. Сверено live на шести тайтлах - позиции
    совпадали до секунды и для фильмов, и для серий, - поэтому локальная
    копия убрана, а данные берутся из одного источника.

    Без `media_id` список не собирается: общую карту просмотренного отдаёт
    `/watching/statuses`, который и так обходит историю KinoPub, и второй
    проход по ней был бы лишним запросом ради тех же данных.
    """
    if not media_id:
        return {'items': []}
    session = await refresh_if_needed(kp_session or '', session_get(kp_session))
    try:
        payload = await kino_get(session, 'v1/watching', {'id': media_id})
    except HTTPException as exc:
        # Пустой список вместо ошибки: без прогресса карточка просто
        # покажет «Смотреть» вместо «Продолжить», а падать целиком из-за
        # недоступного апстрима ей незачем.
        log_event('history', 'Watch progress unavailable', {'media_id': media_id, 'status': exc.status_code})
        return {'items': []}
    return {'items': _progress_rows_from_watching(media_id, payload)}


@app.put('/history')
async def put_history(payload: ProgressPayload, kp_session: Optional[str] = Cookie(default=None)) -> Dict[str, str]:
    completed = payload.completed or (payload.duration > 0 and payload.position / payload.duration >= .9)
    await _mirror_watch_progress(payload, completed, kp_session)
    return {'status': 'ok'}


async def _mirror_watch_progress(payload: ProgressPayload, completed: bool, kp_session: Optional[str]) -> None:
    """Push the same progress to KinoPub's own history, not just this
    bridge's local SQLite - this bridge streams through its own relay/proxy,
    so KinoPub's servers never see playback happen unless told explicitly.
    Best-effort: a failure here must never break the local save, which is
    what "Продолжить" actually depends on.

    `v1/watching/marktime` records the resume position for "История" and
    other devices - verified live that sending `time` at or past the
    video's own duration also flips KinoPub's status to fully-watched on
    its own, so this alone covers ordinary progress and completion.
    `v1/watching/toggle` (the explicit watched/unwatched switch) is layered
    on top only as a fallback for completion, and only after confirming
    KinoPub doesn't already consider the episode watched: `toggle` *flips*
    rather than sets, and the player calls this on both `pause` and `ended`
    for the same finish (pause fires just before ended) - toggling
    unconditionally would flip an already-watched episode back off on the
    second call.
    """
    if not (payload.media_id.isdigit() and payload.episode_number):
        return
    try:
        session = await refresh_if_needed(kp_session or '', session_get(kp_session))
    except HTTPException:
        return
    params: Dict[str, Any] = {'id': payload.media_id, 'video': payload.episode_number}
    if payload.season:
        params['season'] = payload.season
    try:
        marktime_position = payload.duration if (completed and payload.duration) else payload.position
        await kino_get(session, 'v1/watching/marktime', {**params, 'time': int(marktime_position)})
        if completed and payload.episode_id:
            watching_payload = await kino_get(session, 'v1/watching', {'id': payload.media_id})
            raw_item = watching_payload.get('item') if isinstance(watching_payload, dict) else {}
            episodes = _watching_episode_map(raw_item if isinstance(raw_item, dict) else {})
            already_watched = bool(episodes.get(str(payload.episode_id), {}).get('watched'))
            if not already_watched:
                await kino_get(session, 'v1/watching/toggle', params)
    except HTTPException as exc:
        log_event('history', 'Remote watch-mark failed', {
            'media_id': payload.media_id, 'video': payload.episode_number,
            'completed': completed, 'status': exc.status_code,
        })
