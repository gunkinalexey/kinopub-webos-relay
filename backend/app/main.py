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


app = FastAPI(title='KinoPub webOS bridge', version='0.9.75', lifespan=lifespan)
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


class ProgressPayload(BaseModel):
    media_id: str
    episode_id: Optional[str] = None
    position: float = 0
    duration: float = 0
    completed: bool = False


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
async def catalog_list(section: str = 'movie', feed: str = 'fresh', page: int = 0, perpage: int = 48, kp_session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    """Return one explicitly typed KinoPub catalogue section.

    Each sidebar section is sent to KinoPub with its own API ``type`` value,
    rather than being approximated by filtering a mixed movie/serial payload.
    """
    section = section.strip().lower()
    feed = feed.strip().lower()
    selector = section_params(section)
    endpoint = CATALOG_FEEDS.get(feed)
    if not selector:
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
    return {
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
    """Every history item of one type, walking upstream pages until exhausted."""
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
            if item and str(item.get('type') or '') == section:
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
    if section and section not in HISTORY_TYPES:
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
    """The extra fields the details panel shows beyond a catalogue card."""
    duration = _plain_number(_nested_get(raw, 'duration.total')) or _plain_number(_pick_first(raw, ['duration', 'length']))
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
        'director': ', '.join(_name_list(raw.get('director') or raw.get('directors'))),
        'cast': _name_list(raw.get('cast') or raw.get('actors')),
        'duration': int(duration) if duration else 0,
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
# Dict[str, Any], not Dict[str, bool]: FastAPI validates the response against
# this annotation, and `expires_in` is a number. A bool-only model made every
# authenticated call return 500, so reloading the page looked like a lost login.
def auth_status(kp_session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    try:
        row = session_get(kp_session)
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
    session_save(sid, {'access_token': data['access_token'], 'refresh_token': data.get('refresh_token'), 'expires_at': time.time() + int(data.get('expires_in', 3600)) - 60})
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


MOCK_ITEMS: List[Dict[str, Any]] = [
    {'id':'m1','type':'movie','title':'Дюна: Часть вторая','original_title':'Dune: Part Two','year':2024,'rating':8.6,'duration':9960,'genres':['фантастика','драма'],'poster':'linear-gradient(145deg,#8a5b34,#1a130f)','backdrop':'linear-gradient(110deg,#16110d,#8a5b34 75%,#24160d)','description':'Продолжение истории Пола Атрейдеса. Демонстрационная карточка для проверки TV-интерфейса.','streams':[]},
    {'id':'m2','type':'movie','title':'Оппенгеймер','original_title':'Oppenheimer','year':2023,'rating':8.4,'duration':10800,'genres':['драма','история'],'poster':'linear-gradient(145deg,#703425,#160f0d)','backdrop':'linear-gradient(110deg,#0e0d0c,#703425 75%,#1b0d08)','description':'Большая карточка фильма, история просмотра, настройки потока и управление с пульта.','streams':[]},
    {'id':'s1','type':'series','title':'Сёгун','original_title':'Shōgun','year':2024,'rating':8.8,'genres':['драма','история'],'poster':'linear-gradient(145deg,#314f43,#0c1512)','backdrop':'linear-gradient(110deg,#09100d,#315949 75%,#112019)','description':'Сериал с сезонами и сериями. Список подготовлен без привязки к реальному API.','seasons':[{'number':1,'episodes':[{'id':'s1e1','number':1,'title':'Андзин','duration':4200},{'id':'s1e2','number':2,'title':'Слуги двух господ','duration':3900},{'id':'s1e3','number':3,'title':'Завтра наступит завтра','duration':4050}]}],'streams':[]},
    {'id':'m3','type':'movie','title':'Бегущий по лезвию 2049','original_title':'Blade Runner 2049','year':2017,'rating':8.2,'duration':9840,'genres':['фантастика','триллер'],'poster':'linear-gradient(145deg,#8b4d2c,#141217)','backdrop':'linear-gradient(110deg,#0d0b0f,#8b4d2c 70%,#261410)','description':'Контрастный макет для проверки постеров, фокуса и производительности старого webOS.','streams':[]},
    {'id':'s2','type':'series','title':'Разделение','original_title':'Severance','year':2022,'rating':8.7,'genres':['триллер','фантастика'],'poster':'linear-gradient(145deg,#31506f,#0d1319)','backdrop':'linear-gradient(110deg,#091018,#31506f 70%,#101b27)','description':'Демонстрация экранов сериала, продолжения просмотра и перехода к следующей серии.','seasons':[{'number':1,'episodes':[{'id':'s2e1','number':1,'title':'Добрые вести об аде','duration':3420},{'id':'s2e2','number':2,'title':'Половина петли','duration':3300}]}],'streams':[]}
]

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
def get_history(media_id: str = '') -> Dict[str, Any]:
    """Locally stored resume positions, newest first.

    ``media_id`` narrows it to one title so the details screen can offer
    "continue" without pulling the whole list.
    """
    with db_connect() as conn:
        if media_id:
            rows = conn.execute(
                'SELECT * FROM watch_progress WHERE media_id = ? ORDER BY updated_at DESC',
                (media_id,)).fetchall()
        else:
            rows = conn.execute('SELECT * FROM watch_progress ORDER BY updated_at DESC LIMIT 100').fetchall()
    return {'items': [dict(x) for x in rows]}

@app.put('/history')
def put_history(payload: ProgressPayload) -> Dict[str, str]:
    episode=payload.episode_id or ''
    completed=payload.completed or (payload.duration > 0 and payload.position / payload.duration >= .9)
    with db_connect() as conn:
        conn.execute("INSERT INTO watch_progress(media_id,episode_id,position,duration,completed,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(media_id,episode_id) DO UPDATE SET position=excluded.position,duration=excluded.duration,completed=excluded.completed,updated_at=excluded.updated_at", (payload.media_id,episode,float(payload.position),float(payload.duration),1 if completed else 0,time.time()))
    return {'status':'ok'}
