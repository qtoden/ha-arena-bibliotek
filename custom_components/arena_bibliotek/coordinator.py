"""Data update coordinator with an on-disk cache.

One login + overview fetch per refresh. The last good result is saved to HA's storage, so after a
restart (or while the library site is down) the sensors keep the last known loans instead of
becoming unavailable. During an outage it retries every 30 minutes instead of every 6 hours.
"""

from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AccountData, ArenaAuthError, ArenaClient, ArenaError
from .const import DOMAIN, UPDATE_INTERVAL
from .covers import async_prune

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
RETRY_INTERVAL = timedelta(minutes=30)


class ArenaCoordinator(DataUpdateCoordinator[AccountData]):
    """Fetches loans, reservations and fees; falls back to the cached result on outages."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: ArenaClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {entry.title}",
            update_interval=UPDATE_INTERVAL,
            config_entry=entry,
        )
        self.client = client
        self.stale = False  # True while showing cached data because the library could not be reached
        self._store: Store[dict] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._cached: AccountData | None = None

    async def async_load_cache(self) -> None:
        """Load the last saved result (called before the first refresh)."""
        try:
            raw = await self._store.async_load()
            if raw:
                self._cached = AccountData.from_storage(raw)
        except (KeyError, TypeError, ValueError) as err:
            _LOGGER.warning("Ignoring unreadable cache: %s", err)

    async def async_save(self, data: AccountData) -> None:
        self._cached = data
        await self._store.async_save(data.to_storage())

    async def _async_update_data(self) -> AccountData:
        try:
            data = await self.client.fetch()
        except ArenaAuthError as err:
            raise ConfigEntryAuthFailed("Inloggningen nekades – kontrollera kortnummer och PIN") from err
        except ArenaError as err:
            if self._cached is None:
                raise UpdateFailed(str(err)) from err
            # Keep showing the last known data and try again sooner
            if not self.stale:
                _LOGGER.warning(
                    "Library unreachable (%s), showing data from %s; retrying every %s",
                    err, self._cached.fetched_at.isoformat(timespec="minutes"), RETRY_INTERVAL,
                )
            self.stale = True
            self.update_interval = RETRY_INTERVAL
            return self._cached
        self.stale = False
        self.update_interval = UPDATE_INTERVAL
        await self.async_save(data)
        self.hass.async_create_background_task(async_prune(self.hass), f"{DOMAIN} prune covers")
        return data
