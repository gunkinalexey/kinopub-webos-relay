import asyncio
import hashlib
import ipaddress
import json
import os
import secrets
import shutil
import socket
import sqlite3
import time
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

pending_devices: Dict[str, Dict[str, Any]] = {}
debug_events = []
page_count_cache: Dict[str, Dict[str, Any]] = {}
profile_cache: Dict[str, Dict[str, Any]] = {}
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
        conn.execute("CREATE TABLE IF NOT EXISTS user_settings (profile TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS watch_progress (media_id TEXT NOT NULL, episode_id TEXT NOT NULL DEFAULT '', position REAL NOT NULL, duration REAL NOT NULL, completed INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL, PRIMARY KEY(media_id, episode_id))")


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


app = FastAPI(title='KinoPub webOS bridge', version='0.9.60', lifespan=lifespan)
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
    app_icon: str = 'kinopub'


class ProgressPayload(BaseModel):
    media_id: str
    episode_id: Optional[str] = None
    position: float = 0
    duration: float = 0
    completed: bool = False


class AudioHlsPayload(BaseModel):
    url: str
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
    if not poster and item_id.isdigit():
        poster = f'https://m.staticpop.net/poster/item/medium/{item_id}.jpg'
    backdrop = _image_url(raw.get('background') or raw.get('backdrop') or raw.get('covers') or raw.get('posters'), 'big') or poster
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
        'description': str(_pick_first(raw, ['plot', 'description', 'overview'], '')),
        'watched': _extract_watched_status(raw),
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
                'file': raw.get('file'),
            })
    return result


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

    def merge_unique_list(target: List[Any], incoming: List[Any], key_builder) -> None:
        known = {key_builder(value) for value in target}
        for value in incoming:
            marker = key_builder(value)
            if marker not in known:
                target.append(value)
                known.add(marker)

    def stream_key(value: Any) -> str:
        if not isinstance(value, dict):
            return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
        return '|'.join(str(value.get(name) or '') for name in ('url', 'file', 'source_type', 'quality', 'height', 'codec'))

    def audio_key(value: Any) -> str:
        if not isinstance(value, dict):
            return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
        # ID is the most stable key; index is the FFmpeg stream number used by
        # the player. Include both because either one can be absent.
        return '|'.join(str(value.get(name) or '') for name in ('id', 'index', 'track_index', 'stream_index', 'lang'))

    def subtitle_key(value: Any) -> str:
        if not isinstance(value, dict):
            return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
        return '|'.join(str(value.get(name) or '') for name in ('url', 'file', 'lang', 'shift', 'embed'))

    def track_numbers(value: Any) -> List[str]:
        if isinstance(value, list):
            raw_values = value
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

    return out


def choose_best_stream(streams: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not streams:
        return None
    def score(stream: Dict[str, Any]) -> tuple:
        protocol_score = {'hls': 0, 'hls2': 1, 'http': 2, 'hls4': 3}.get(str(stream.get('source_type')), 4)
        codec = str(stream.get('codec') or '').lower()
        codec_score = 0 if codec in {'h264', 'avc', 'avc1', ''} else 2
        height = int(stream.get('height') or 0)
        height_penalty = abs((height or 1080) - 1080)
        return (codec_score, protocol_score, height_penalty)
    return sorted(streams, key=score)[0]


async def kino_get(session: Dict[str, Any], path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    query = dict(params or {})
    query['access_token'] = session['access_token']
    try:
        response = await app.state.http.get(f"{API_BASE}/{path.lstrip('/')}", params=query, headers={'Accept': 'application/json'})
    except httpx.TimeoutException as exc:
        raise HTTPException(504, 'KinoPub API timeout') from exc
    except httpx.RequestError as exc:
        raise HTTPException(502, f'Could not connect to KinoPub API: {exc}') from exc
    if response.status_code >= 400:
        raise HTTPException(response.status_code, response.text[:1000])
    try:
        return response.json()
    except ValueError as exc:
        raise HTTPException(502, 'KinoPub API returned invalid JSON') from exc


@app.get('/catalog/home')
async def catalog_home(kp_session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    session = await refresh_if_needed(kp_session or '', session_get(kp_session))
    requests = [
        ('popular', 'Популярные', 'v1/items/popular', {'type': 'movie', 'page': 0, 'perpage': 30}),
        ('fresh', 'Свежие', 'v1/items/fresh', {'type': 'movie', 'page': 0, 'perpage': 30}),
        ('hot', 'Горячие', 'v1/items/hot', {'type': 'movie', 'page': 0, 'perpage': 30}),
        ('series', 'Сериалы', 'v1/items/fresh', {'type': 'serial', 'page': 0, 'perpage': 30}),
    ]
    rows = []
    for row_id, title, path, params in requests:
        try:
            payload = await kino_get(session, path, params)
            items = extract_catalog_items(payload)
            rows.append({'id': row_id, 'title': title, 'items': items})
        except HTTPException as exc:
            log_event('catalog', f'Could not load {row_id}', {'status': exc.status_code})
            rows.append({'id': row_id, 'title': title, 'items': []})
    hero = next((item for row in rows for item in row['items']), None)
    return {'hero': hero, 'rows': rows}



CATALOG_SECTION_TYPES = {
    'movie': 'movie',
    'serial': 'serial',
    'anime': 'anime',
    'concert': 'concert',
    'documovie': 'documovie',
    'docuserial': 'docuserial',
    'tvshow': 'tvshow',
    'sport': 'sport',
}
CATALOG_FEEDS = {
    'popular': 'v1/items/popular',
    'fresh': 'v1/items/fresh',
    'hot': 'v1/items/hot',
    'all': 'v1/items',
}

@app.get('/catalog/list')
async def catalog_list(section: str = 'movie', feed: str = 'fresh', page: int = 0, perpage: int = 48, kp_session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    """Return one explicitly typed KinoPub catalogue section.

    Each sidebar section is sent to KinoPub with its own API ``type`` value,
    rather than being approximated by filtering a mixed movie/serial payload.
    """
    section = section.strip().lower()
    feed = feed.strip().lower()
    api_type = CATALOG_SECTION_TYPES.get(section)
    endpoint = CATALOG_FEEDS.get(feed)
    if not api_type:
        raise HTTPException(400, f'Unknown catalogue section: {section}')
    if not endpoint:
        raise HTTPException(400, f'Unknown catalogue feed: {feed}')
    # The UI uses zero-based indexes internally, while KinoPub's catalogue
    # treats page=1 as the first page. page=0 is accepted upstream but aliases
    # page=1, which previously made UI pages 1 and 2 show identical results.
    page = max(0, min(page, 9999))
    api_page = page + 1
    perpage = max(1, min(perpage, 100))
    session = await refresh_if_needed(kp_session or '', session_get(kp_session))
    payload = await kino_get(session, endpoint, {'type': api_type, 'page': api_page, 'perpage': perpage})
    items = extract_catalog_items(payload)
    for item in items:
        item['section'] = section
    log_event('catalog', 'Catalogue section loaded', {
        'section': section, 'api_type': api_type, 'feed': feed, 'page': page, 'api_page': api_page, 'count': len(items)
    })
    pagination = payload.get('pagination') if isinstance(payload, dict) and isinstance(payload.get('pagination'), dict) else {}
    if not pagination and isinstance(payload, dict):
        pagination = payload.get('meta') if isinstance(payload.get('meta'), dict) else {}

    def first_int(*values):
        for value in values:
            try:
                if value is not None and str(value) != '':
                    return int(value)
            except (TypeError, ValueError):
                pass
        return 0

    # KinoPub uses pagination.total as the number of pages. The item count,
    # when present, is exposed separately as total_count / total_items.
    total_pages = first_int(
        pagination.get('total'), pagination.get('pages'), pagination.get('total_pages'),
        pagination.get('page_count'), pagination.get('last_page'),
        payload.get('pages') if isinstance(payload, dict) else None,
        payload.get('total_pages') if isinstance(payload, dict) else None,
    )
    total_items = first_int(
        pagination.get('total_count'), pagination.get('total_items'), pagination.get('items_count'),
        payload.get('total_count') if isinstance(payload, dict) else None,
        payload.get('total_items') if isinstance(payload, dict) else None,
    )
    if not total_items and total_pages:
        # This is an upper-bound estimate until the final page is loaded.
        total_items = total_pages * perpage
    # A full page is evidence that a following page may exist. Some KinoPub
    # shortcut responses omit totals or expose stale/ambiguous pagination data.
    has_next = (page + 1 < total_pages) if total_pages > 1 else (len(items) >= perpage)
    return {
        'section': section,
        'type': api_type,
        'feed': feed,
        'page': page,
        'perpage': perpage,
        'total_items': total_items,
        'total_pages': total_pages,
        'has_next': has_next,
        'items': items,
    }



def _page_count_cache_key(section: str, feed: str, perpage: int) -> str:
    return f"{section}:{feed}:{perpage}"


def _pagination_values(payload: Any, perpage: int) -> Dict[str, int]:
    pagination = payload.get('pagination') if isinstance(payload, dict) and isinstance(payload.get('pagination'), dict) else {}
    if not pagination and isinstance(payload, dict) and isinstance(payload.get('meta'), dict):
        pagination = payload.get('meta')

    def first_int(*values: Any) -> int:
        for value in values:
            try:
                if value is not None and str(value) != '':
                    return int(value)
            except (TypeError, ValueError):
                pass
        return 0

    total_pages = first_int(
        pagination.get('total'), pagination.get('pages'), pagination.get('total_pages'),
        pagination.get('page_count'), pagination.get('last_page'),
        payload.get('pages') if isinstance(payload, dict) else None,
        payload.get('total_pages') if isinstance(payload, dict) else None,
    )
    total_items = first_int(
        pagination.get('total_count'), pagination.get('total_items'), pagination.get('items_count'),
        payload.get('total_count') if isinstance(payload, dict) else None,
        payload.get('total_items') if isinstance(payload, dict) else None,
    )
    if not total_items and total_pages:
        total_items = total_pages * perpage
    return {'total_pages': max(0, total_pages), 'total_items': max(0, total_items)}


async def _catalog_page_probe(session: Dict[str, Any], endpoint: str, api_type: str, page: int, perpage: int) -> Dict[str, Any]:
    payload = await kino_get(session, endpoint, {
        'type': api_type, 'page': page, 'perpage': perpage
    })
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
    api_type = CATALOG_SECTION_TYPES[section]
    endpoint = CATALOG_FEEDS[feed]
    key = _page_count_cache_key(section, feed, perpage)
    cached = page_count_cache.get(key)
    if cached and not refresh and cached.get('expires_at', 0) > time.time():
        return {k: v for k, v in cached.items() if k != 'expires_at'}

    probe_cache: Dict[int, Dict[str, Any]] = {}
    probes = 0

    async def probe(page: int) -> Dict[str, Any]:
        nonlocal probes
        page = max(1, page)
        if page not in probe_cache:
            probe_cache[page] = await _catalog_page_probe(session, endpoint, api_type, page, perpage)
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

    page_count_cache[key] = {**result, 'expires_at': time.time() + 6 * 60 * 60}
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
            ('movie', 'all'), ('serial', 'all'), ('anime', 'all'), ('concert', 'all'),
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
    if section not in CATALOG_SECTION_TYPES:
        raise HTTPException(400, f'Unknown catalogue section: {section}')
    if feed not in CATALOG_FEEDS:
        raise HTTPException(400, f'Unknown catalogue feed: {feed}')
    perpage = max(1, min(perpage, 100))
    session = await refresh_if_needed(kp_session or '', session_get(kp_session))
    return await _discover_page_count(session, section, feed, perpage, refresh=refresh)


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


@app.get('/catalog/search')
async def catalog_search(q: str, mode: str = 'all', kp_session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    query = q.strip()
    if not query:
        return {'query': '', 'mode': mode, 'items': []}
    session = await refresh_if_needed(kp_session or '', session_get(kp_session))
    # The public API exposes a general search endpoint. The mode is retained in
    # the response/UI; title mode additionally narrows obvious non-title hits.
    payload = await kino_get(session, 'v1/items/search', {'q': query, 'page': 0, 'perpage': 60})
    items = extract_catalog_items(payload)
    if mode == 'title':
        q_lower = query.lower()
        narrowed = [item for item in items if q_lower in str(item.get('title', '')).lower() or q_lower in str(item.get('original_title', '')).lower()]
        if narrowed:
            items = narrowed
    return {'query': query, 'mode': mode, 'items': items}


@app.get('/catalog/items/{item_id}')
async def catalog_item(item_id: str, kp_session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    session = await refresh_if_needed(kp_session or '', session_get(kp_session))
    payload = await kino_get(session, f'v1/items/{item_id}', {})
    raw_item = payload.get('item') if isinstance(payload, dict) and isinstance(payload.get('item'), dict) else payload
    item = normalize_catalog_item(raw_item if isinstance(raw_item, dict) else {'id': item_id})
    media = collect_media(payload)
    item['media'] = media
    item['seasons'] = []
    seasons: Dict[str, Dict[str, Any]] = {}
    for entry in media:
        season_no = str(entry.get('season') or 1)
        seasons.setdefault(season_no, {'number': entry.get('season') or 1, 'episodes': []})['episodes'].append(entry)
    item['seasons'] = list(seasons.values())
    return item


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
    if not streams and not str(selected['id']).startswith('direct-'):
        links = await kino_get(session, 'v1/items/media-links', {'mid': selected['id']})
        files = links.get('files') if isinstance(links, dict) else []
        if isinstance(files, list):
            for file_value in files:
                if isinstance(file_value, dict):
                    streams.extend(stream_from_file(file_value))
        if isinstance(links, dict) and isinstance(links.get('subtitles'), list):
            subtitles = links['subtitles']
        if isinstance(links, dict) and isinstance(links.get('audios'), list):
            audios = links['audios']
    best = choose_best_stream(streams)
    if not best:
        raise HTTPException(404, 'KinoPub returned no compatible stream URL')
    log_event('media', 'Play option resolved', {'item_id': item_id, 'media_id': selected['id'], 'protocol': best.get('source_type'), 'quality': best.get('quality'), 'codec': best.get('codec'), 'audio_count': len(audios), 'tracks': selected.get('tracks')})
    return {'item_id': item_id, 'media': selected, 'streams': streams, 'selected': best, 'subtitles': subtitles, 'audios': audios}


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
    return {'status': 'ok', 'version': app.version, 'credentials_configured': bool(CLIENT_ID and CLIENT_SECRET)}




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

@app.get('/auth/status')
def auth_status(kp_session: Optional[str] = Cookie(default=None)) -> Dict[str, bool]:
    try:
        session_get(kp_session)
        return {'authenticated': True, 'credentials_configured': bool(CLIENT_ID and CLIENT_SECRET)}
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
    session_save(sid, {'access_token': data['access_token'], 'refresh_token': data.get('refresh_token'), 'expires_at': time.time() + int(data.get('expires_in', 3600)) - 60})
    pending_devices.pop(payload.code, None)
    response.set_cookie('kp_session', sid, httponly=True, secure=COOKIE_SECURE, samesite='lax', max_age=60 * 60 * 24 * 30, path='/')
    log_event('auth', 'Device authorized')
    previous_task = getattr(app.state, 'page_count_task', None)
    if previous_task and not previous_task.done():
        previous_task.cancel()
    app.state.page_count_task = asyncio.create_task(prewarm_page_counts(force=True, sid=sid))
    return JSONResponse({'status': 'authorized'}, headers=dict(response.headers))


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



@app.get('/image')
async def image_proxy(
    url: str,
    request: Request,
    width: int = 0,
    height: int = 0,
    quality: int = 82,
):
    """Same-origin image proxy with optional resizing for TV browsers.

    Poster requests use a small fixed size, while detail backdrops use a larger
    size. JPEG output is broadly compatible with older webOS browsers and is
    substantially smaller than the original source images.
    """
    safe_url = await validate_stream_url(url)
    width = max(0, min(int(width or 0), 1920))
    height = max(0, min(int(height or 0), 1080))
    quality = max(55, min(int(quality or 82), 92))
    headers = {
        'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
        'User-Agent': request.headers.get('user-agent', 'Mozilla/5.0'),
    }
    if IMAGE_REFERER:
        headers['Referer'] = IMAGE_REFERER
    try:
        upstream = await app.state.http.get(safe_url, headers=headers, follow_redirects=True)
    except httpx.TimeoutException as exc:
        raise HTTPException(504, 'Image request timed out') from exc
    except httpx.RequestError as exc:
        raise HTTPException(502, f'Could not load image: {exc}') from exc
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

    response_headers = {
        'Cache-Control': 'public, max-age=2592000, immutable',
    }
    log_event('image', 'Image proxied', {
        'host': urlparse(safe_url).hostname,
        'bytes': len(content),
        'width': width,
        'height': height,
        'quality': quality,
    })
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
    if offset <= 0:
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
    return PlainTextResponse(subtitle_to_vtt(text, max(0.0, float(offset or 0))), media_type='text/vtt; charset=utf-8', headers={'Cache-Control': 'no-store'})

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


def _ffmpeg_http_reconnect_options() -> List[str]:
    """Make long KinoPub CDN reads survive premature TLS/HTTP disconnects."""
    return [
        '-seekable', '1',
        '-reconnect', '1',
        '-reconnect_on_network_error', '1',
        '-reconnect_on_http_error', '429,500,502,503,504',
        '-reconnect_streamed', '1',
        '-reconnect_delay_max', '2',
        '-reconnect_max_retries', '30',
        '-reconnect_delay_total_max', '60',
        '-respect_retry_after', '1',
        '-rw_timeout', '30000000',
    ]


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


async def _inspect_audio_source(url: str, headers: Dict[str, str], requested: int) -> Dict[str, Any]:
    """Resolve the KinoPub audio index to an absolute FFmpeg stream index."""
    command = ['ffprobe', '-v', 'error', *_ffmpeg_http_reconnect_options()]
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
        log_event('media', 'Audio HLS probe failed', {'track': requested, 'error': detail})
        raise HTTPException(502, 'Could not inspect audio tracks in the HTTP source')
    try:
        payload = json.loads(stdout.decode('utf-8', errors='replace') or '{}')
    except json.JSONDecodeError as exc:
        raise HTTPException(502, 'Invalid FFprobe response') from exc
    streams = payload.get('streams') if isinstance(payload, dict) else []
    audio_streams = [stream for stream in streams or [] if stream.get('codec_type') == 'audio']
    selected = next((stream for stream in audio_streams if int(stream.get('index', -1)) == requested), None)
    if selected is None and 1 <= requested <= len(audio_streams):
        selected = audio_streams[requested - 1]
    if selected is None:
        available = [int(stream.get('index', -1)) for stream in audio_streams]
        log_event('media', 'Audio HLS track missing', {'requested': requested, 'available': available})
        raise HTTPException(409, f'Audio stream {requested} is not present in this quality')
    duration = 0.0
    try:
        duration = max(0.0, float((payload.get('format') or {}).get('duration') or 0))
    except (TypeError, ValueError):
        duration = 0.0
    return {'stream': selected, 'duration': duration}


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
    resolved_track = int(selected['index'])
    requested_start = max(0.0, float(payload.start or 0))
    bucket_start = float(int(requested_start // AUDIO_HLS_START_BUCKET) * AUDIO_HLS_START_BUCKET)
    source_key = hashlib.sha256(final_url.encode('utf-8')).hexdigest()
    session_key = hashlib.sha256(sid.encode('utf-8')).hexdigest()[:16]
    job_key = f'{session_key}:{source_key}:{resolved_track}:{bucket_start:.3f}'
    existing_id = audio_hls_job_keys.get(job_key)
    existing = audio_hls_jobs.get(existing_id or '')
    if existing and existing.get('status') not in {'failed', 'stopped'}:
        existing['touched'] = time.time()
        return _audio_job_public(existing)

    job_id = secrets.token_urlsafe(12).replace('-', '').replace('_', '')
    job_dir = AUDIO_HLS_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=False)
    playlist = job_dir / 'index.m3u8'
    stderr_path = job_dir / 'ffmpeg.log'
    segment_pattern = str(job_dir / 'segment_%06d.ts')
    raw_headers = _ffmpeg_headers(headers)
    command = ['ffmpeg', '-hide_banner', '-loglevel', 'warning', '-nostdin']
    command += _ffmpeg_http_reconnect_options()
    if bucket_start > 0:
        command += ['-ss', f'{bucket_start:.3f}']
    if raw_headers:
        command += ['-headers', raw_headers]
    command += [
        '-fflags', '+genpts+discardcorrupt',
        '-i', final_url,
        '-map', '0:v:0',
        '-map', f'0:{resolved_track}',
        '-sn', '-dn',
        '-c:v', 'copy',
        '-bsf:v', 'h264_mp4toannexb',
        '-c:a', 'aac',
        '-profile:a', 'aac_low',
        '-ar', '48000',
        '-b:a', '192k',
        '-ac', '2',
        '-af', 'aresample=async=1:first_pts=0',
        '-max_muxing_queue_size', '2048',
        '-avoid_negative_ts', 'make_zero',
        '-muxdelay', '0',
        '-muxpreload', '0',
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
        'track': resolved_track,
        'duration': inspected.get('duration', 0),
    }
    audio_hls_jobs[job_id] = job
    audio_hls_job_keys[job_key] = job_id
    asyncio.create_task(_monitor_audio_hls_job(job_id))
    log_event('media', 'Audio HLS started', {
        'job': job_id,
        'host': urlparse(final_url).hostname,
        'requested_track': payload.track,
        'resolved_track': resolved_track,
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


MOCK_ITEMS: List[Dict[str, Any]] = [
    {'id':'m1','type':'movie','title':'Дюна: Часть вторая','original_title':'Dune: Part Two','year':2024,'rating':8.6,'duration':9960,'genres':['фантастика','драма'],'poster':'linear-gradient(145deg,#8a5b34,#1a130f)','backdrop':'linear-gradient(110deg,#16110d,#8a5b34 75%,#24160d)','description':'Продолжение истории Пола Атрейдеса. Демонстрационная карточка для проверки TV-интерфейса.','streams':[]},
    {'id':'m2','type':'movie','title':'Оппенгеймер','original_title':'Oppenheimer','year':2023,'rating':8.4,'duration':10800,'genres':['драма','история'],'poster':'linear-gradient(145deg,#703425,#160f0d)','backdrop':'linear-gradient(110deg,#0e0d0c,#703425 75%,#1b0d08)','description':'Большая карточка фильма, история просмотра, настройки потока и управление с пульта.','streams':[]},
    {'id':'s1','type':'series','title':'Сёгун','original_title':'Shōgun','year':2024,'rating':8.8,'genres':['драма','история'],'poster':'linear-gradient(145deg,#314f43,#0c1512)','backdrop':'linear-gradient(110deg,#09100d,#315949 75%,#112019)','description':'Сериал с сезонами и сериями. Список подготовлен без привязки к реальному API.','seasons':[{'number':1,'episodes':[{'id':'s1e1','number':1,'title':'Андзин','duration':4200},{'id':'s1e2','number':2,'title':'Слуги двух господ','duration':3900},{'id':'s1e3','number':3,'title':'Завтра наступит завтра','duration':4050}]}],'streams':[]},
    {'id':'m3','type':'movie','title':'Бегущий по лезвию 2049','original_title':'Blade Runner 2049','year':2017,'rating':8.2,'duration':9840,'genres':['фантастика','триллер'],'poster':'linear-gradient(145deg,#8b4d2c,#141217)','backdrop':'linear-gradient(110deg,#0d0b0f,#8b4d2c 70%,#261410)','description':'Контрастный макет для проверки постеров, фокуса и производительности старого webOS.','streams':[]},
    {'id':'s2','type':'series','title':'Разделение','original_title':'Severance','year':2022,'rating':8.7,'genres':['триллер','фантастика'],'poster':'linear-gradient(145deg,#31506f,#0d1319)','backdrop':'linear-gradient(110deg,#091018,#31506f 70%,#101b27)','description':'Демонстрация экранов сериала, продолжения просмотра и перехода к следующей серии.','seasons':[{'number':1,'episodes':[{'id':'s2e1','number':1,'title':'Добрые вести об аде','duration':3420},{'id':'s2e2','number':2,'title':'Половина петли','duration':3300}]}],'streams':[]}
]

@app.get('/mock/home')
def mock_home() -> Dict[str, Any]:
    return {'hero': MOCK_ITEMS[0], 'rows': [
        {'id':'continue','title':'Продолжить просмотр','items':MOCK_ITEMS[1:4]},
        {'id':'popular','title':'Популярное','items':MOCK_ITEMS},
        {'id':'series','title':'Сериалы','items':[x for x in MOCK_ITEMS if x['type']=='series']}
    ]}

@app.get('/mock/items/{media_id}')
def mock_item(media_id: str) -> Dict[str, Any]:
    for item in MOCK_ITEMS:
        if item['id'] == media_id:
            return item
    raise HTTPException(404, 'Mock item not found')

@app.get('/mock/search')
def mock_search(q: str = '') -> Dict[str, Any]:
    needle=q.strip().lower()
    items=[x for x in MOCK_ITEMS if not needle or needle in x['title'].lower() or needle in x.get('original_title','').lower()]
    return {'items':items}

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


@app.get('/history')
def get_history() -> Dict[str, Any]:
    with db_connect() as conn:
        rows=conn.execute('SELECT * FROM watch_progress ORDER BY updated_at DESC LIMIT 100').fetchall()
    return {'items':[dict(x) for x in rows]}

@app.put('/history')
def put_history(payload: ProgressPayload) -> Dict[str, str]:
    episode=payload.episode_id or ''
    completed=payload.completed or (payload.duration > 0 and payload.position / payload.duration >= .9)
    with db_connect() as conn:
        conn.execute("INSERT INTO watch_progress(media_id,episode_id,position,duration,completed,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(media_id,episode_id) DO UPDATE SET position=excluded.position,duration=excluded.duration,completed=excluded.completed,updated_at=excluded.updated_at", (payload.media_id,episode,float(payload.position),float(payload.duration),1 if completed else 0,time.time()))
    return {'status':'ok'}
