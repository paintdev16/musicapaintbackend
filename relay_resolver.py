"""Resolve YouTube audio through public Piped/Invidious relays.

This is a best-effort first rung for hosts whose datacenter IP is blocked by
YouTube. The normal yt-dlp resolver remains the fallback.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
from urllib.parse import urlencode, urlparse

import httpx

logger = logging.getLogger(__name__)

_PIPED = (
    "https://api.piped.private.coffee",
    "https://pipedapi.adminforge.de",
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.leptons.xyz",
)
_INVIDIOUS = (
    "https://inv.nadeko.net",
    "https://invidious.jing.rocks",
)
_TTL = 300
_TIMEOUT = 2.5
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
_instances: tuple[list[str], list[str]] | None = None
_instances_at = 0.0
_breaker_until = 0.0


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().rstrip(".")


def _same_service_domain(host: str, api_host: str) -> bool:
    if host == api_host:
        return True
    # Piped API and its media proxy commonly use sibling subdomains.
    api_root = ".".join(api_host.split(".")[-2:])
    return host == api_root or host.endswith("." + api_root)


def _public_host(host: str) -> bool:
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
        return bool(addresses) and all(ipaddress.ip_address(address).is_global for address in addresses)
    except (OSError, ValueError):
        return False


async def _safe_instance(instance: str) -> bool:
    parsed = urlparse(instance)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host or parsed.username or parsed.password or parsed.port not in (None, 443):
        return False
    return await asyncio.to_thread(_public_host, host)


async def _get_instances() -> tuple[list[str], list[str]]:
    global _instances, _instances_at
    now = time.monotonic()
    if _instances is not None and now - _instances_at < _TTL:
        return _instances

    piped: list[str] = []
    invidious: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=1.5, follow_redirects=False) as client:
            response = await client.get("https://raw.githubusercontent.com/n-ce/Uma/main/dynamic_instances.json")
            if response.is_success:
                payload = response.json()
                piped = [item.rstrip("/") for item in payload.get("piped", []) if isinstance(item, str) and item.startswith("https://")]
                invidious = [item.rstrip("/") for item in payload.get("invidious", []) if isinstance(item, str) and item.startswith("https://")]
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.debug("dynamic relay list unavailable: %s", exc)

    _instances = (piped or list(_PIPED), invidious or list(_INVIDIOUS))
    _instances_at = now
    return _instances


async def _safe_audio_url(url: str, api_host: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        return False
    if host != "googlevideo.com" and not host.endswith(".googlevideo.com") and not _same_service_domain(host, api_host):
        return False
    return await asyncio.to_thread(_public_host, host)


async def _piped_audio(client: httpx.AsyncClient, instance: str, video_id: str) -> dict | None:
    api_host = _host(instance)
    try:
        if not await _safe_instance(instance):
            return None
        response = await client.get(f"{instance}/streams/{video_id}", headers={"User-Agent": _UA})
        if not response.is_success:
            return None
        data = response.json()
        streams = [item for item in data.get("audioStreams", []) if isinstance(item, dict) and str(item.get("mimeType", "")).startswith("audio") and item.get("url")]
        streams.sort(key=lambda item: item.get("bitrate") or 0, reverse=True)
        for stream in streams:
            url = stream["url"]
            if await _safe_audio_url(url, api_host):
                return {"url": url, "allowed_stream_suffix": ".".join(api_host.split(".")[-2:]), "ext": "webm", "acodec": "unknown", "id": video_id, "relay": True, "http_headers": {"User-Agent": _UA}}
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
        logger.debug("Piped relay failed (%s): %s", api_host, exc)
    return None


async def _invidious_audio(client: httpx.AsyncClient, instance: str, video_id: str) -> dict | None:
    api_host = _host(instance)
    try:
        if not await _safe_instance(instance):
            return None
        response = await client.get(f"{instance}/api/v1/videos/{video_id}", headers={"User-Agent": _UA})
        if not response.is_success:
            return None
        formats = [item for item in response.json().get("adaptiveFormats", []) if isinstance(item, dict) and str(item.get("type", "")).startswith("audio") and item.get("itag")]
        formats.sort(key=lambda item: item.get("bitrate") or 0, reverse=True)
        for fmt in formats:
            url = f"{instance}/latest_version?{urlencode({'id': video_id, 'itag': fmt['itag']})}"
            if await _safe_audio_url(url, api_host):
                return {"url": url, "allowed_stream_suffix": ".".join(api_host.split(".")[-2:]), "ext": "webm", "acodec": "unknown", "id": video_id, "relay": True, "http_headers": {"User-Agent": _UA}}
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
        logger.debug("Invidious relay failed (%s): %s", api_host, exc)
    return None


async def resolve_audio(video_id: str) -> dict | None:
    """Return a validated relay audio URL, or None when the relay fleet fails."""
    global _breaker_until
    now = time.monotonic()
    if now < _breaker_until:
        return None

    piped, invidious = await _get_instances()
    timeout = httpx.Timeout(_TIMEOUT)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        tasks = [
            asyncio.create_task(resolver(client, instance.rstrip("/"), video_id))
            for instances, resolver in ((piped[:3], _piped_audio), (invidious[:3], _invidious_audio))
            for instance in instances
        ]
        try:
            for task in asyncio.as_completed(tasks):
                try:
                    result = await task
                except Exception:
                    continue
                if isinstance(result, dict):
                    for pending in tasks:
                        if not pending.done():
                            pending.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    _breaker_until = 0.0
                    return result
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    _breaker_until = time.monotonic() + 600
    return None
