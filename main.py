import asyncio
from importlib.util import find_spec
import logging
import os
import random
import re
import shutil
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, urlparse

import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel

from relay_resolver import resolve_audio as resolve_relay_audio

app = FastAPI(title="Music API", docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)
logger = logging.getLogger(__name__)

def configured_cors_origins() -> list[str]:
    configured = os.getenv("CORS_ALLOW_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000,http://localhost:3000,http://localhost:5173")
    origins = [origin.strip() for origin in configured.split(",") if origin.strip()]
    if "null" not in origins:
        origins.append("null")
    return origins


app.add_middleware(
    CORSMiddleware,
    allow_origins=configured_cors_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Range"],
)

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
MIX_CONTENT_RE = re.compile(
    r"\b(mix|megamix|mashup|dj\s+set|full\s+album|playlist|non[\s-]?stop|\d+\s+hour|\d+\s+hora)\b",
    re.IGNORECASE,
)

# Cache en memoria para búsquedas (TTL de 15 minutos)
_SEARCH_CACHE: dict[str, tuple[float, list[dict]]] = {}
_SEARCH_CACHE_LOCK = Lock()
CACHE_TTL = 900
CACHE_MAX_ENTRIES = 256
_AUDIO_CACHE: dict[str, tuple[float, dict]] = {}
_AUDIO_REQUEST_LOCKS: dict[str, asyncio.Lock] = {}
_AUDIO_CACHE_LOCK = asyncio.Lock()
AUDIO_CACHE_TTL = 900
AUDIO_CACHE_MAX_ENTRIES = 512

_YTDLP_TOKEN_LOG_RE = re.compile(
    r"generating a (gvs|player|subs) po token for ([a-z0-9_-]+) client via ([a-z0-9_-]+)",
    re.IGNORECASE,
)


def _audio_error_category(error: object) -> str:
    message = str(error).lower()
    if "sign in to confirm" in message or "confirm you're not a bot" in message or "login_required" in message:
        return "youtube_login_or_ip_block"
    if "http error 403" in message or "http 403" in message:
        return "http_403"
    if "requested format is not available" in message or "no video formats found" in message:
        return "no_compatible_audio_format"
    if "private video" in message or "video is unavailable" in message or "not available in your country" in message:
        return "video_unavailable_or_restricted"
    if "timed out" in message or "timeout" in message:
        return "network_timeout"
    if (
        "name or service not known" in message
        or "temporary failure in name resolution" in message
        or "failed to resolve" in message
    ):
        return "dns_error"
    return "other_extraction_error"


class _AudioYtDlpLogger:
    """Keep only PO-token diagnostics from yt-dlp; never log signed media URLs."""

    def __init__(self, video_id: str, client_name: str):
        self.video_id = video_id
        self.client_name = client_name
        self.provider_list_reported = False
        self.token_requested = False

    def debug(self, message: str) -> None:
        if "po token providers:" in message.lower():
            self.provider_list_reported = True
            logger.info(
                "yt-dlp PO-token providers listed video=%s client=%s",
                self.video_id,
                self.client_name,
            )
            return
        match = _YTDLP_TOKEN_LOG_RE.search(message)
        if match:
            self.token_requested = True
            logger.info(
                "yt-dlp requested PO token video=%s client=%s context=%s provider=%s",
                self.video_id,
                self.client_name,
                match.group(1).lower(),
                match.group(3).lower(),
            )

    def warning(self, message: str) -> None:
        # Warnings can contain signed URLs. The final exception is classified
        # below without writing the raw message to Render logs.
        return None

    def error(self, message: str) -> None:
        return None

    def screen(self, message: str) -> None:
        return None

    def write(self, message: str) -> None:
        return None


def normalize_video_id(value: str) -> str:
    value = value.strip()
    if VIDEO_ID_RE.fullmatch(value):
        return value
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(400, "URL inválida")
    host = (parsed.hostname or "").lower()
    if host in {"youtube.com", "www.youtube.com", "music.youtube.com"} and parsed.path == "/watch":
        video_id = parse_qs(parsed.query).get("v", [""])[0]
    elif host == "youtu.be":
        video_id = parsed.path.strip("/")
    else:
        raise HTTPException(400, "Solo se aceptan enlaces de videos de YouTube")
    if not VIDEO_ID_RE.fullmatch(video_id):
        raise HTTPException(400, "ID de video inválido")
    return video_id


def ydl_options(**extra):
    runtimes = {}
    deno_name = "deno.exe" if os.name == "nt" else "deno"
    deno_path = shutil.which("deno") or str(Path.home() / ".deno" / "bin" / deno_name)
    if not Path(deno_path).is_file():
        deno_path = None
    node_path = shutil.which("node")
    if deno_path:
        runtimes["deno"] = {"path": deno_path}
    elif node_path:
        runtimes["node"] = {"path": node_path}

    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        **extra,
    }
    if runtimes:
        options["js_runtimes"] = runtimes
    return options


def extract_audio(video_id: str) -> dict:
    video_id = normalize_video_id(video_id)
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    # default and android_vr do not require a PO token. mweb is included
    # because BgUtil can supply its GVS token. tv is a final token-free
    # fallback. tv_embedded needs account cookies, and android uses Android
    # attestation tokens that the BotGuard provider does not generate.
    attempts = (
        ("default", {}),
        ("android_vr", {"extractor_args": {"youtube": {"player_client": ["android_vr"]}}}),
        ("mweb", {"extractor_args": {"youtube": {"player_client": ["mweb"]}}}),
        ("tv", {"extractor_args": {"youtube": {"player_client": ["tv"]}}}),
    )
    # The player consumes an audio stream; avoid the final muxed `best`
    # fallback because some browsers reject a video container on <audio>.
    format_selector = "bestaudio[ext=m4a]/bestaudio[ext=mp4]/bestaudio"
    last_error = None
    for client_name, attempt in attempts:
        ydl_logger = _AudioYtDlpLogger(video_id, client_name)
        try:
            with yt_dlp.YoutubeDL(ydl_options(
                format=format_selector,
                socket_timeout=12,
                extractor_retries=1,
                verbose=True,
                logger=ydl_logger,
                **attempt,
            )) as ydl:
                info = ydl.extract_info(video_url, download=False)
            if info and info.get("url") and info.get("acodec") not in (None, "none"):
                logger.info(
                    "YouTube audio resolved video=%s client=%s ext=%s codec=%s token_requested=%s",
                    video_id,
                    client_name,
                    info.get("ext") or "unknown",
                    info.get("acodec") or "unknown",
                    ydl_logger.token_requested,
                )
                return info
            last_error = RuntimeError("YouTube no devolvió un formato con audio")
            failure_category = _audio_error_category(last_error)
        except Exception as exc:
            last_error = exc
            failure_category = _audio_error_category(exc)
        logger.warning(
            "YouTube audio extraction failed video=%s client=%s category=%s provider_list=%s token_requested=%s",
            video_id,
            client_name,
            failure_category,
            ydl_logger.provider_list_reported,
            ydl_logger.token_requested,
        )

    if last_error:
        raise last_error
    raise HTTPException(404, "Audio no disponible")


async def get_audio(video_id: str) -> dict:
    video_id = normalize_video_id(video_id)
    now = time.time()
    async with _AUDIO_CACHE_LOCK:
        cached = _AUDIO_CACHE.get(video_id)
        if cached and cached[0] > now:
            return cached[1].copy()
        if cached:
            _AUDIO_CACHE.pop(video_id, None)
        key_lock = _AUDIO_REQUEST_LOCKS.setdefault(video_id, asyncio.Lock())

    async with key_lock:
        now = time.time()
        async with _AUDIO_CACHE_LOCK:
            cached = _AUDIO_CACHE.get(video_id)
            if cached and cached[0] > now:
                return cached[1].copy()
        try:
            data = await resolve_relay_audio(video_id)
        except Exception as exc:
            logger.warning("Audio relay resolution failed for %s: %s", video_id, exc)
            data = None
        if data is None:
            try:
                data = await asyncio.to_thread(extract_audio, video_id)
            except HTTPException:
                raise
            except Exception as exc:
                logger.exception("Audio extraction failed for video %s", video_id)
                raise HTTPException(502, "No se pudo extraer el audio del video") from exc

        ttl = AUDIO_CACHE_TTL
        expires = parse_qs(urlparse(data.get("url", "")).query).get("expire", [])
        if expires:
            try:
                ttl = min(ttl, int(expires[0]) - int(time.time()) - 30)
            except ValueError:
                pass
        if ttl > 0:
            async with _AUDIO_CACHE_LOCK:
                _AUDIO_CACHE[video_id] = (time.time() + ttl, data)
                while len(_AUDIO_CACHE) > AUDIO_CACHE_MAX_ENTRIES:
                    _AUDIO_CACHE.pop(next(iter(_AUDIO_CACHE)))
                if len(_AUDIO_REQUEST_LOCKS) > AUDIO_CACHE_MAX_ENTRIES:
                    for stale_id, stale_lock in list(_AUDIO_REQUEST_LOCKS.items()):
                        if stale_id not in _AUDIO_CACHE and not stale_lock.locked():
                            _AUDIO_REQUEST_LOCKS.pop(stale_id, None)
                            if len(_AUDIO_REQUEST_LOCKS) <= AUDIO_CACHE_MAX_ENTRIES:
                                break
        return data.copy()


GENRE_RULES = [
    ("cumbia", ("cumbia", "cumbion", "sonidera", "tecnocumbia", "grupo 5", "grupo5", "armonia 10", "armonía 10", "agua marina", "corazon serrano")),
    ("bachata", ("bachata", "romeo santos", "prince royce", "aventura")),
    ("salsa", ("salsa", "salsa romantica", "salsa romántica", "marc anthony", "grupo niche", "el gran combo")),
    ("vallenato", ("vallenato", "silvestre dangond", "binomio de oro", "rafael orozco")),
    ("merengue", ("merengue", "juan luis guerra", "elvis crespo")),
    ("bolero", ("bolero", "boleros", "trio los panchos")),
    ("balada", ("balada", "baladas", "ballad", "romanticas", "románticas", "luis miguel", "alejandro sanz", "ricardo arjona", "camila", "sin bandera")),
    ("reggaeton", ("reggaeton", "reguetón", "urbano", "perreo", "dembow", "daddy yankee", "bad bunny", "tony dize", "plan b", "wisin", "yandel", "don omar", "j balvin", "rauw alejandro", "ivy queen")),
    ("regional mexicano", ("regional mexicano", "banda", "norteño", "norteno", "corridos", "ranchera", "peso pluma", "grupo frontera", "banda ms")),
    ("lofi", ("lofi", "lo-fi", "chill beats")),
    ("rock", ("rock", "metal", "punk", "ac dc", "led zeppelin", "nirvana", "men at work", "dire straits")),
    ("hip hop", ("hip hop", "hip-hop", "rap", "trap", "eminem", "50 cent", "kendrick lamar", "drake")),
    ("electronica", ("electronic", "electrónica", "edm", "techno", "house", "trance", "synthwave")),
    ("jazz", ("jazz", "swing", "bebop")),
    ("reggae", ("reggae", "dancehall")),
    ("country", ("country", "bluegrass", "americana")),
    ("clasica", ("classical", "clásica", "sinfonica", "sinfónica", "orquesta filarmónica")),
    ("pop", ("pop", "pop latino", "dance pop")),
]


def classify_genre(title: str = "", artist: str = "", supplied: str | None = None) -> str | None:
    generic_genres = {"music", "música", "musica", "latin", "latino", "world", "world music", "unknown", "desconocido", "various artists"}
    if supplied:
        supplied_text = re.sub(r"[^a-z0-9áéíóúñü]+", " ", supplied.casefold())
        for genre, clues in GENRE_RULES:
            if any(clue.casefold() in supplied_text for clue in clues):
                return genre
        normalized_supplied = supplied.strip().casefold()
        if normalized_supplied not in generic_genres:
            return normalized_supplied

    text = f"{title} {artist}".casefold()
    text = re.sub(r"[^a-z0-9áéíóúñü]+", " ", text)
    for genre, clues in GENRE_RULES:
        if any(clue.casefold() in text for clue in clues):
            return genre
    return None


def song_from_entry(entry: dict) -> dict | None:
    video_id = entry.get("id")
    if not isinstance(video_id, str) or not VIDEO_ID_RE.fullmatch(video_id):
        return None
    thumbnails = entry.get("thumbnails") or []
    thumbnail = next((t.get("url") for t in reversed(thumbnails) if t.get("url")), None)
    title = entry.get("title") or "Sin título"
    artist = entry.get("artist") or entry.get("channel") or entry.get("uploader") or "Desconocido"
    genre_value = entry.get("genre") or entry.get("genres")
    if isinstance(genre_value, list):
        genre_value = next((value for value in genre_value if isinstance(value, str)), None)
    return {
        "id": video_id,
        "title": title,
        "artist": artist,
        "genre": classify_genre(title, artist, genre_value if isinstance(genre_value, str) else None),
        "duration": entry.get("duration"),
        "thumbnail": thumbnail or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        "url": f"https://www.youtube.com/watch?v={video_id}",
    }


def search_youtube(query: str, limit: int) -> list[dict]:
    query_clean = query.strip()
    cache_key = f"{query_clean.lower()}:{limit}"
    now = time.time()
    with _SEARCH_CACHE_LOCK:
        cached = _SEARCH_CACHE.get(cache_key)
        if cached and now - cached[0] < CACHE_TTL:
            return [song.copy() for song in cached[1]]
        if cached:
            _SEARCH_CACHE.pop(cache_key, None)

    with yt_dlp.YoutubeDL(ydl_options(extract_flat=True)) as ydl:
        data = ydl.extract_info(f"ytsearch{limit}:{query_clean}", download=False)
    results = []
    for entry in (data or {}).get("entries") or []:
        if not entry:
            continue
        song = song_from_entry(entry)
        if song:
            if not song["genre"]:
                song["genre"] = classify_genre(query_clean)
            results.append(song)

    with _SEARCH_CACHE_LOCK:
        expired = [key for key, (timestamp, _) in _SEARCH_CACHE.items() if now - timestamp >= CACHE_TTL]
        for key in expired:
            _SEARCH_CACHE.pop(key, None)
        while len(_SEARCH_CACHE) >= CACHE_MAX_ENTRIES:
            _SEARCH_CACHE.pop(next(iter(_SEARCH_CACHE)))
        _SEARCH_CACHE[cache_key] = (now, results)
    return [song.copy() for song in results]


def related_searches(
    video_id: str,
    limit: int,
    exclude: set[str],
    seed_title: str | None = None,
    seed_artist: str | None = None,
    seed_genre: str | None = None,
) -> list[dict]:
    title = (seed_title or "").strip()
    artist = (seed_artist or "").strip()
    if not title:
        with yt_dlp.YoutubeDL(ydl_options(extract_flat=True)) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        if not info:
            return []
        title = (info.get("track") or info.get("title") or "").strip()
        artist = artist or (info.get("artist") or info.get("creator") or info.get("uploader") or info.get("channel") or "").strip()
    if not title:
        return []

    genre = classify_genre(title, artist, seed_genre)

    if genre:
        queries = [
            f"canciones de {genre} exitosas",
            f"mejores canciones de {genre}",
            f"canciones populares de {genre}",
        ]
    else:
        queries = [
            f"canciones similares a {title}",
            f"música parecida a {title}",
            f"canciones del estilo de {title}",
        ]
    unique = set(exclude) | {video_id}
    candidates: dict[str, dict] = {}
    search_limit = min(50, max(25, limit))
    with ThreadPoolExecutor(max_workers=len(queries)) as executor:
        query_results = list(executor.map(lambda query: search_youtube(query, search_limit), queries))

    for songs in query_results:
        for song in songs:
            song_id = song["id"]
            candidate_title_text = song.get("title", "")
            duration = song.get("duration")
            if (
                song_id in unique
                or MIX_CONTENT_RE.search(candidate_title_text)
                or (isinstance(duration, (int, float)) and duration > 600)
            ):
                continue
            candidate_artist_text = song.get("artist", "")
            candidate_genre = classify_genre(candidate_title_text, candidate_artist_text, song.get("genre"))
            if genre and candidate_genre and candidate_genre != genre:
                continue
            if genre and not song.get("genre"):
                song = {**song, "genre": genre}
            candidates.setdefault(song_id, song)

    recommendations = list(candidates.values())
    random.shuffle(recommendations)
    max_per_channel = max(2, min(4, (limit + 24) // 25))
    channel_counts: dict[str, int] = {}
    diversified = []
    for song in recommendations:
        channel = re.sub(r"\s+", " ", song.get("artist", "").strip().casefold()) or song["id"]
        if channel_counts.get(channel, 0) >= max_per_channel:
            continue
        channel_counts[channel] = channel_counts.get(channel, 0) + 1
        diversified.append(song)
        if len(diversified) >= limit:
            break
    return diversified


@app.get("/")
def home():
    return {
        "message": "Music API funcionando",
        "docs": "/docs",
        "endpoints": {
            "search": "/search?q=...",
            "suggestions": "/suggest?q=...",
            "metadata": "/info?url=...",
            "related": "/related/{video_id}",
            "audio": "/direct/{video_id}",
            "audio_proxy": "/proxy/{video_id}",
        },
    }


@app.get("/health")
@app.get("/health/ready", include_in_schema=False)
@app.get("/api/health")
def health():
    deno_name = "deno.exe" if os.name == "nt" else "deno"
    deno_path = shutil.which("deno") or str(Path.home() / ".deno" / "bin" / deno_name)
    if not Path(deno_path).is_file():
        deno_path = None
    node_path = shutil.which("node")
    runtime = None
    runtime_version = None
    runtime_path = deno_path or node_path
    if deno_path:
        runtime = "deno"
    elif node_path:
        runtime = "node"
    if runtime_path:
        try:
            version = subprocess.run(
                [runtime_path, "--version"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            runtime_version = (version.stdout or version.stderr).strip().splitlines()[0][:40]
        except (OSError, subprocess.SubprocessError, IndexError):
            runtime_version = "unavailable"
    try:
        with socket.create_connection(("127.0.0.1", 4416), timeout=0.25):
            pot_provider = True
    except OSError:
        pot_provider = False
    return {
        "status": "ok",
        "audio": {
            "yt_dlp": yt_dlp.version.__version__,
            "ejs_installed": find_spec("yt_dlp_ejs") is not None,
            "js_runtime": runtime,
            "js_runtime_version": runtime_version,
            "pot_provider": pot_provider,
        },
    }


@app.get("/player", include_in_schema=False)
def player():
    return FileResponse(Path(__file__).parent / "static" / "player.html")


@app.get("/search")
async def search(q: str = Query(min_length=1, max_length=100), limit: int = Query(15, ge=1, le=30)):
    if not q.strip():
        raise HTTPException(400, "La búsqueda está vacía")
    try:
        results = await asyncio.to_thread(search_youtube, q.strip(), limit)
    except Exception as exc:
        raise HTTPException(502, "No se pudo completar la búsqueda") from exc
    return {"query": q, "total": len(results), "results": results}


def _api_track(song: dict) -> dict:
    video_id = song["id"]
    artist_name = song.get("artist") or "Desconocido"
    artwork = song.get("thumbnail") or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    artist = {"id": "", "name": artist_name, "imageUrl": artwork, "genres": []}
    return {
        "id": video_id,
        "title": song.get("title") or "Sin título",
        "artist": artist,
        "album": {"id": "", "title": "", "artist": artist, "artworkUrl": artwork, "releaseYear": 0, "trackCount": 0},
        "artworkUrl": artwork,
        "duration": float(song.get("duration") or 0),
        "streamUrl": f"/api/stream/{video_id}/audio",
        "isDownloaded": False,
        "isLiked": False,
        "youtubeId": video_id,
    }


def _api_search_youtube_music(query: str, filter_name: str | None) -> dict:
    from ytmusicapi import YTMusic

    client = YTMusic()
    normalized_filter = "songs" if filter_name in {"tracks", "songs"} else filter_name
    raw_results = client.search(query, filter=normalized_filter, limit=20)
    tracks = []
    albums = []
    artists = []
    playlists = []

    def artwork(item: dict) -> str:
        thumbs = item.get("thumbnails") or []
        return (thumbs[-1].get("url") if thumbs else "") or ""

    def artist_payload(items: list, fallback: str = "") -> dict:
        first = next((item for item in items if isinstance(item, dict)), {})
        return {"id": first.get("id") or "", "name": first.get("name") or fallback or "Desconocido", "imageUrl": "", "genres": []}

    for item in raw_results:
        kind = item.get("resultType") or ("song" if item.get("videoId") else "")
        thumb = artwork(item)
        if kind in {"song", "video"} and item.get("videoId"):
            artist = artist_payload(item.get("artists") or [])
            album_item = item.get("album") or {}
            if isinstance(album_item, str):
                album_item = {"name": album_item}
            duration = item.get("duration") or "0:00"
            parts = duration.split(":") if isinstance(duration, str) else []
            seconds = 0
            try:
                if len(parts) == 2:
                    seconds = int(parts[0]) * 60 + int(parts[1])
                elif len(parts) == 3:
                    seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                elif isinstance(duration, (int, float)):
                    seconds = float(duration)
            except ValueError:
                pass
            tracks.append({
                "id": item["videoId"], "title": item.get("title") or "Sin título", "artist": artist,
                "album": {"id": album_item.get("id") or "", "title": album_item.get("name") or "", "artist": artist, "artworkUrl": thumb, "releaseYear": 0, "trackCount": 0},
                "artworkUrl": thumb, "duration": seconds, "streamUrl": f"/api/stream/{item['videoId']}/audio",
                "isDownloaded": False, "isLiked": False, "youtubeId": item["videoId"],
            })
        elif kind == "album" and item.get("browseId"):
            albums.append({"id": item["browseId"], "title": item.get("title") or "", "artist": artist_payload(item.get("artists") or []), "artworkUrl": thumb, "releaseYear": item.get("year") or 0, "trackCount": 0})
        elif kind == "artist" and item.get("browseId"):
            artists.append({"id": item["browseId"], "name": item.get("artist") or item.get("name") or "", "imageUrl": thumb, "genres": []})
        elif kind == "playlist" and item.get("browseId"):
            playlists.append({"id": item["browseId"], "title": item.get("title") or "", "artworkUrl": thumb, "trackCount": 0, "source": "youtube"})
    return {"tracks": tracks, "albums": albums, "artists": artists, "playlists": playlists}


@app.get("/api/search")
async def api_search(
    q: str = Query(min_length=1, max_length=200),
    filter: str | None = Query(None, pattern="^(tracks|songs|albums|artists|playlists)$"),
):
    query = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", q).strip()
    if not query:
        raise HTTPException(400, "La búsqueda está vacía")
    try:
        results = await asyncio.to_thread(_api_search_youtube_music, query, filter)
    except Exception as exc:
        logger.warning("YouTube Music API search failed: %s", exc)
        if filter in {"albums", "artists", "playlists"}:
            raise HTTPException(502, "No se pudo completar la búsqueda") from exc
        try:
            songs = await asyncio.to_thread(search_youtube, query, 20)
            results = {"tracks": [_api_track(song) for song in songs], "albums": [], "artists": [], "playlists": []}
        except Exception as fallback_exc:
            raise HTTPException(502, "No se pudo completar la búsqueda") from fallback_exc
    return {"query": query, **results}


@app.get("/api/search/suggest")
async def api_search_suggest(q: str = Query(default="", max_length=200)):
    query = q.strip()
    if len(query) < 2:
        return []
    try:
        songs = await asyncio.to_thread(search_youtube, query, 7)
    except Exception as exc:
        raise HTTPException(502, "No se pudieron cargar las sugerencias") from exc
    return list(dict.fromkeys(song["title"] for song in songs if song.get("title")))


class ResolveRequest(BaseModel):
    url: str


@app.post("/api/search/resolve")
async def api_search_resolve(body: ResolveRequest):
    video_id = normalize_video_id(body.url)
    try:
        def resolve_metadata():
            with yt_dlp.YoutubeDL(ydl_options(extract_flat=True)) as ydl:
                return ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)

        info = await asyncio.to_thread(resolve_metadata)
    except Exception as exc:
        raise HTTPException(502, "No se pudo resolver el video") from exc
    song = song_from_entry(info or {"id": video_id})
    if not song:
        raise HTTPException(404, "Video no encontrado")
    return {"query": body.url, "type": "track", "tracks": [_api_track(song)], "albums": [], "artists": [], "playlists": []}


@app.get("/api/health/live", include_in_schema=False)
@app.get("/health/live", include_in_schema=False)
def health_live():
    return {"status": "ok"}


@app.get("/suggest")
async def suggest(q: str = Query(min_length=2, max_length=100), limit: int = Query(7, ge=1, le=10)):
    if not q.strip():
        return {"query": q, "results": []}
    try:
        results = await asyncio.to_thread(search_youtube, q.strip(), limit)
    except Exception as exc:
        raise HTTPException(502, "No se pudieron cargar las sugerencias") from exc
    return {"query": q, "results": results}


@app.get("/related/{video_id}")
async def related(
    video_id: str,
    limit: int = Query(15, ge=1, le=100),
    exclude: str = Query("", max_length=1200),
    title: str | None = Query(None, max_length=200),
    artist: str | None = Query(None, max_length=200),
    genre: str | None = Query(None, max_length=80),
):
    video_id = normalize_video_id(video_id)
    excluded = {x for x in exclude.split(",") if VIDEO_ID_RE.fullmatch(x)}
    try:
        results = await asyncio.to_thread(related_searches, video_id, limit, excluded, title, artist, genre)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, "No se pudieron buscar canciones relacionadas") from exc
    return {"video_id": video_id, "total": len(results), "results": results}


@app.get("/api/recommendations/autoplay")
async def api_autoplay(
    track_id: str = Query(min_length=11, max_length=11),
    limit: int = Query(20, ge=1, le=100),
    title: str | None = Query(None, max_length=200),
    artist: str | None = Query(None, max_length=200),
    genre: str | None = Query(None, max_length=80),
):
    video_id = normalize_video_id(track_id)
    try:
        songs = await asyncio.to_thread(related_searches, video_id, limit, set(), title, artist, genre)
    except Exception as exc:
        raise HTTPException(502, "No se pudieron buscar canciones relacionadas") from exc
    return {"tracks": [_api_track(song) for song in songs]}


@app.get("/info")
async def info(url: str):
    data = await get_audio(normalize_video_id(url))
    return {
        "id": data.get("id"),
        "title": data.get("title"),
        "artist": data.get("artist") or data.get("uploader"),
        "duration": data.get("duration"),
        "thumbnail": data.get("thumbnail"),
        "format": data.get("ext"),
        "codec": data.get("acodec"),
    }


@app.get("/stream")
async def stream(url: str):
    return await direct(normalize_video_id(url))


@app.get("/direct/{video_id}")
async def direct(video_id: str):
    data = await get_audio(video_id)
    if data.get("relay"):
        return RedirectResponse(f"/proxy/{normalize_video_id(video_id)}", status_code=307)
    return RedirectResponse(data["url"], status_code=307)


@app.post("/warmup/{video_id}")
@app.post("/api/stream/{video_id}/warm")
async def warmup_audio(video_id: str):
    normalized_id = normalize_video_id(video_id)
    await get_audio(normalized_id)
    return {"status": "ready", "ok": True, "state": "cached"}


@app.get("/proxy/{video_id}")
@app.get("/api/stream/{video_id}/audio")
async def proxy(video_id: str, request: Request):
    data = await get_audio(video_id)
    audio_url = data["url"]
    parsed = urlparse(audio_url)
    host = (parsed.hostname or "").lower()
    allowed_suffix = str(data.get("allowed_stream_suffix") or "").lower().lstrip(".")

    def host_allowed(candidate: str) -> bool:
        candidate = candidate.lower().rstrip(".")
        return (
            candidate == "googlevideo.com"
            or candidate.endswith(".googlevideo.com")
            or bool(allowed_suffix and (candidate == allowed_suffix or candidate.endswith("." + allowed_suffix)))
        )

    if parsed.scheme != "https" or not host_allowed(host) or parsed.username or parsed.password:
        raise HTTPException(502, "Destino de audio no permitido")
    source_headers = data.get("http_headers") or {}
    headers = {k: v for k, v in source_headers.items() if k.lower() in {"user-agent", "referer", "origin", "accept", "accept-language"}}
    byte_range = request.headers.get("range")
    if byte_range:
        if not re.fullmatch(r"bytes=\d*-\d*", byte_range) or byte_range == "bytes=-":
            raise HTTPException(400, "Cabecera Range inválida")
        headers["Range"] = byte_range
    client = httpx.AsyncClient(follow_redirects=False, timeout=httpx.Timeout(connect=15, read=60, write=15, pool=15))
    try:
        for _ in range(3):
            upstream = await client.send(client.build_request(request.method, audio_url, headers=headers), stream=True)
            if upstream.status_code not in {301, 302, 303, 307, 308}:
                break
            location = upstream.headers.get("location")
            await upstream.aclose()
            if not location:
                raise HTTPException(502, "El origen de audio devolvió una redirección inválida")
            audio_url = httpx.URL(audio_url).join(location).__str__()
            redirected = urlparse(audio_url)
            redirect_host = (redirected.hostname or "").lower()
            if redirected.scheme != "https" or not host_allowed(redirect_host) or redirected.username or redirected.password:
                raise HTTPException(502, "El origen de audio redirigió a un destino no permitido")
        else:
            raise HTTPException(502, "Demasiadas redirecciones del origen de audio")
    except HTTPException:
        await client.aclose()
        raise
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(502, "No se pudo conectar con el origen de audio") from exc
    if upstream.status_code not in (200, 206):
        status = upstream.status_code
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(502, f"Origen respondió HTTP {status}")

    async def chunks():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    response_headers = {k: v for k, v in upstream.headers.items() if k.lower() in {"content-length", "content-range", "accept-ranges"}}
    return StreamingResponse(
        chunks(),
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/octet-stream"),
        headers=response_headers,
    )


@app.head("/proxy/{video_id}", include_in_schema=False)
@app.head("/api/stream/{video_id}/audio", include_in_schema=False)
async def proxy_head(video_id: str, request: Request):
    return await proxy(video_id, request)


