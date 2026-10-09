"""Resolve HA media and stream decoded PCM into the existing call media owner."""

from __future__ import annotations

import asyncio
from contextlib import aclosing, suppress
from collections.abc import AsyncGenerator
from urllib.parse import urlsplit

from homeassistant.components import ffmpeg, media_source
from homeassistant.components.media_player import async_process_play_media_url
from homeassistant.exceptions import HomeAssistantError
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession


async def _input_chunks(hass: HomeAssistant, media_id: str) -> AsyncGenerator[bytes]:
    path = None
    url = media_id
    if media_source.is_media_source_id(media_id):
        item = await media_source.async_resolve_media(hass, media_id, None)
        if not item.mime_type.startswith("audio/"):
            raise HomeAssistantError("Select an audio file")
        path, url = item.path, item.url
    if path is not None:
        # Only HA's resolver can supply a local path. Arbitrary filesystem paths
        # and file:// inputs are not accepted by the public action.
        opening = hass.async_add_executor_job(path.open, "rb")
        try:
            handle = await asyncio.shield(opening)
        except asyncio.CancelledError:
            # Executor work cannot be cancelled once it has started. Reclaim
            # the handle even if the call ends while the file is opening.
            handle = await opening
            await hass.async_add_executor_job(handle.close)
            raise
        try:
            while chunk := await hass.async_add_executor_job(handle.read, 16384):
                yield chunk
        finally:
            await hass.async_add_executor_job(handle.close)
        return
    url = async_process_play_media_url(hass, url)
    if urlsplit(url).scheme not in {"http", "https"}:
        raise HomeAssistantError("Use an HA media source or an HTTP(S) audio URL")
    async with async_get_clientsession(hass).get(url) as response:
        response.raise_for_status()
        async for chunk in response.content.iter_chunked(16384):
            yield chunk


async def async_audio_stream(hass: HomeAssistant, media_id: str) -> AsyncGenerator[bytes]:
    """Yield bounded 16 kHz mono s16le chunks; release decoder on cancellation."""
    process = await asyncio.create_subprocess_exec(
        ffmpeg.get_ffmpeg_manager(hass).binary,
        "-hide_banner", "-loglevel", "error", "-nostdin",
        # Fetching and authentication belong to HA. Do not let a downloaded
        # playlist cause FFmpeg to open more URLs or local files itself.
        "-protocol_whitelist", "pipe", "-i", "pipe:0",
        "-map", "0:a:0", "-vn", "-sn", "-dn",
        "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )

    async def feed():
        try:
            async with aclosing(_input_chunks(hass, media_id)) as chunks:
                async for chunk in chunks:
                    process.stdin.write(chunk)
                    await process.stdin.drain()
        finally:
            process.stdin.close()

    writer = asyncio.create_task(feed(), name="voip-announcement-input")
    produced = False
    try:
        while chunk := await process.stdout.read(4096):
            produced = True
            yield chunk
        await writer
        if await process.wait() != 0 or not produced:
            raise HomeAssistantError("Media could not be decoded as audio")
    finally:
        writer.cancel()
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        await asyncio.gather(writer, return_exceptions=True)
        await process.wait()
