"""Axiell Arena library integration (loans, reservations, fees, renewals)."""

from __future__ import annotations

from datetime import date, timedelta
import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .api import ArenaClient, ArenaError, Loan
from .const import CONF_BASE_URL, DOMAIN
from .coordinator import ArenaCoordinator
from .covers import CoverView, async_check_size, async_clear

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BUTTON]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_RENEW = "renew"
SERVICE_CLEAR_COVERS = "clear_covers"
EVENT_RENEWED = f"{DOMAIN}_renewed"
ATTR_ENTRY = "config_entry_id"
ATTR_TITLES = "titles"
ATTR_DUE_WITHIN = "due_within_days"
ATTR_DRY_RUN = "dry_run"

RENEW_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTRY): cv.string,
        vol.Optional(ATTR_TITLES): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(ATTR_DUE_WITHIN): vol.All(vol.Coerce(int), vol.Range(min=0, max=365)),
        vol.Optional(ATTR_DRY_RUN, default=False): cv.boolean,
    }
)

type ArenaConfigEntry = ConfigEntry[ArenaCoordinator]


def make_chooser(titles: list[str] | None, due_within_days: int | None):
    """Which loans to renew: matching titles (case-insensitive, partial), due soon, or all."""
    wanted = [t.casefold() for t in titles or []]
    limit = date.today() + timedelta(days=due_within_days) if due_within_days is not None else None

    def choose(loan: Loan) -> bool:
        if wanted and not any(w in loan.title.casefold() for w in wanted):
            return False
        if limit and (loan.due_date is None or loan.due_date > limit):
            return False
        return True

    return choose


async def async_renew(
    hass: HomeAssistant, entry: ArenaConfigEntry, choose, dry_run: bool = False
) -> list[dict]:
    """Renew via the entry's client, push fresh data to the sensors and fire an event."""
    coordinator = entry.runtime_data
    try:
        outcomes, after = await coordinator.client.renew(choose, dry_run=dry_run)
    except ArenaError as err:
        raise HomeAssistantError(f"Omlån misslyckades: {err}") from err
    if after is not None and not dry_run:
        await coordinator.async_save(after)
        coordinator.stale = False
        coordinator.async_set_updated_data(after)
    results = [o.as_dict() for o in outcomes]
    if not dry_run:
        hass.bus.async_fire(
            EVENT_RENEWED,
            {
                "entry_id": entry.entry_id,
                "renewed": sum(1 for r in results if r["renewed"]),
                "failed": sum(1 for r in results if not r["renewed"]),
                "results": results,
            },
        )
    return results


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the actions and the cover images once for all library cards."""
    await async_check_size(hass)
    hass.http.register_view(CoverView(hass))

    async def handle_clear_covers(call: ServiceCall) -> ServiceResponse:
        return {"removed": await async_clear(hass)}

    hass.services.async_register(
        DOMAIN, SERVICE_CLEAR_COVERS, handle_clear_covers, supports_response=SupportsResponse.OPTIONAL
    )

    async def handle_renew(call: ServiceCall) -> ServiceResponse:
        entries: list[ArenaConfigEntry] = [
            e for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED
        ]
        if entry_id := call.data.get(ATTR_ENTRY):
            entries = [e for e in entries if e.entry_id == entry_id]
        if not entries:
            raise ServiceValidationError("Inget inläst bibliotekskort hittades")
        choose = make_chooser(call.data.get(ATTR_TITLES), call.data.get(ATTR_DUE_WITHIN))
        response = {}
        for entry in entries:
            response[entry.title] = await async_renew(hass, entry, choose, call.data[ATTR_DRY_RUN])
        return {"results": response}

    hass.services.async_register(
        DOMAIN, SERVICE_RENEW, handle_renew, schema=RENEW_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ArenaConfigEntry) -> bool:
    client = ArenaClient(entry.data[CONF_BASE_URL], entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])
    coordinator = ArenaCoordinator(hass, entry, client)
    await coordinator.async_load_cache()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ArenaConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
