"""Book covers, fetched from the library once and then served by Home Assistant.

Arena serves covers without caching headers, so linking to them directly would fetch them again
on every page view. Instead the sensors link to /api/arena_bibliotek/cover/<record id>. The
first request fetches the cover from the library, shrinks it and keeps it in HA's cache
directory; later requests are answered from there, with a long browser cache time.

Only records that are on a loaded card's loans or reservations are fetched, so the view cannot be
used to make HA fetch anything else. It needs no login, like HA's own brand images, because an
<img> cannot send a token; covers are public on the library's site anyway. Records without a
cover are remembered for a week. Covers of records that left every list are removed after 30 days.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
from http import HTTPStatus
import io
import logging
from pathlib import Path
import re
import time
from urllib.parse import quote

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from .api import ArenaError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

VALID_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
MAX_SIZE = (320, 240)  # width, height in pixels: 3x a small thumbnail
NO_COVER_RETRY = timedelta(days=7)
KEEP_UNUSED = timedelta(days=30)
CACHE_HEADERS = {"Cache-Control": "public, max-age=2592000, immutable"}


def cover_url(record_id: str | None) -> str | None:
    """The sensor attribute for a record's cover."""
    if not record_id or not VALID_ID.match(record_id):
        return None
    return f"/api/{DOMAIN}/cover/{quote(record_id, safe='')}"


def _cover_dir(hass: HomeAssistant) -> Path:
    cache_path = getattr(hass.config, "cache_path", None)  # HA 2025.x and later
    return Path(cache_path(DOMAIN, "covers") if cache_path else hass.config.path(".cache", DOMAIN, "covers"))


def _file_name(record_id: str) -> str:
    return hashlib.sha1(record_id.encode()).hexdigest()


def _shrink(data: bytes) -> bytes:
    """A smaller JPEG; the original if Pillow cannot read it."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            image = image.convert("RGB")
            image.thumbnail(MAX_SIZE)
            out = io.BytesIO()
            image.save(out, "JPEG", quality=82, optimize=True)
            return out.getvalue()
    except Exception as err:  # noqa: BLE001 - any unreadable image is kept as it came
        _LOGGER.debug("Keeping cover unshrunk: %s", err)
        return data


def _referenced(hass: HomeAssistant) -> dict[str, tuple]:
    """Record id -> (coordinator, agency) for every record on a loaded card."""
    found: dict[str, tuple] = {}
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is not ConfigEntryState.LOADED:
            continue
        coordinator = entry.runtime_data
        data = coordinator.data
        if data is None or not data.agency:
            continue
        for item in [*data.loans, *data.reservations]:
            if item.record_id:
                found[item.record_id] = (coordinator, data.agency)
    return found


class CoverView(HomeAssistantView):
    """Serves cached covers, fetching each one from the library the first time."""

    url = f"/api/{DOMAIN}/cover/{{record_id}}"
    name = f"api:{DOMAIN}:cover"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.dir = _cover_dir(hass)
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, request: web.Request, record_id: str) -> web.StreamResponse:
        if not VALID_ID.match(record_id):
            return web.Response(status=HTTPStatus.NOT_FOUND)
        path = self.dir / f"{_file_name(record_id)}.jpg"
        lock = self._locks.setdefault(record_id, asyncio.Lock())
        async with lock:
            body = await self.hass.async_add_executor_job(self._read, path)
            if body is None:
                body = await self._fetch(record_id, path)
        self._locks.pop(record_id, None)
        if body is None:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        return web.Response(body=body, content_type="image/jpeg", headers=CACHE_HEADERS)

    @staticmethod
    def _read(path: Path) -> bytes | None:
        return path.read_bytes() if path.is_file() else None

    async def _fetch(self, record_id: str, path: Path) -> bytes | None:
        owner = _referenced(self.hass).get(record_id)
        if owner is None:
            return None
        marker = path.with_suffix(".none")
        if await self.hass.async_add_executor_job(self._recent, marker):
            return None
        coordinator, agency = owner
        try:
            data = await coordinator.client.fetch_cover(record_id, agency)
        except ArenaError as err:
            _LOGGER.debug("No cover for %s: %s", record_id, err)
            return None  # a network problem: try again on the next request
        if data is None:
            await self.hass.async_add_executor_job(self._write, marker, b"")
            return None
        body = await self.hass.async_add_executor_job(_shrink, data)
        await self.hass.async_add_executor_job(self._write, path, body)
        return body

    @staticmethod
    def _recent(marker: Path) -> bool:
        return marker.is_file() and time.time() - marker.stat().st_mtime < NO_COVER_RETRY.total_seconds()

    @staticmethod
    def _write(path: Path, body: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(body)
        tmp.replace(path)


async def async_prune(hass: HomeAssistant) -> None:
    """Remove covers of records that are on no card's lists, once they are 30 days old."""
    keep = {_file_name(record_id) for record_id in _referenced(hass)}
    folder = _cover_dir(hass)

    def prune() -> None:
        if not folder.is_dir():
            return
        cutoff = time.time() - KEEP_UNUSED.total_seconds()
        for file in folder.iterdir():
            if file.stem not in keep and file.stat().st_mtime < cutoff:
                file.unlink(missing_ok=True)

    await hass.async_add_executor_job(prune)
