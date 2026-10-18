import asyncio
import logging
import os
import random
import re
import shutil
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

app = FastAPI(title="Music API")
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
    attempts = [
        {},
        {"extractor_args": {"youtube": {"player_client": ["tv_simply", "android_vr", "web_embedded"]}}},
    ]
    last_error = None
    for attempt in attempts:
        try:
            with yt_dlp.YoutubeDL(ydl_options(format="bestaudio/best", **attempt)) as ydl:
                info = ydl.extract_info(video_url, download=False)
            if info and info.get("url") and info.get("acodec") != "none":
                return info
            last_error = RuntimeError("YouTube no devolvió un formato con audio")
        except Exception as exc:
            last_error = exc
            logger.warning(
                "YouTube audio extraction attempt failed for %s (%s): %s",
                video_id,
                "default" if not attempt else "fallback clients",
                exc,
            )

    if last_error:
        raise last_error
    raise HTTPException(404, "Audio no disponible")


async def get_audio(video_id: str) -> dict:
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
def health():
    return {"status": "ok"}


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
    return RedirectResponse(data["url"], status_code=307)


@app.post("/warmup/{video_id}")
async def warmup_audio(video_id: str):
    await get_audio(normalize_video_id(video_id))
    return {"status": "ready"}


@app.get("/proxy/{video_id}")
async def proxy(video_id: str, request: Request):
    data = await get_audio(video_id)
    audio_url = data["url"]
    parsed = urlparse(audio_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "googlevideo.com" or host.endswith(".googlevideo.com")):
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
        upstream = await client.send(client.build_request("GET", audio_url, headers=headers), stream=True)
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


