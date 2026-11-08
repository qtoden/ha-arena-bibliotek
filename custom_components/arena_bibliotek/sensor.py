"""Sensors: loans, next due date, reservations ready for pickup, fees."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import ArenaConfigEntry
from .api import AccountData
from .const import DOMAIN
from .coordinator import ArenaCoordinator
from .covers import cover_url


def _next_due(d: AccountData) -> date | None:
    dates = [l.due_date for l in d.loans if l.due_date]
    return min(dates) if dates else None


def _loans_attrs(d: AccountData) -> dict[str, Any]:
    nxt = _next_due(d)
    return {
        "loans": [{**l.as_dict(), "cover": cover_url(l.record_id)} for l in d.loans],
        "overdue": sum(1 for l in d.loans if l.overdue),
        "due_next": [l.title for l in d.loans if l.due_date and l.due_date == nxt],
        "not_renewable": [l.title for l in d.loans if not l.renewable],
    }


def _next_due_attrs(d: AccountData) -> dict[str, Any]:
    nxt = _next_due(d)
    titles = [l.title for l in d.loans if l.due_date and l.due_date == nxt]
    return {
        "titles": titles,
        "days_left": (nxt - date.today()).days if nxt else None,
        "all_renewable": all(l.renewable for l in d.loans if l.due_date == nxt) if nxt else None,
    }


@dataclass(frozen=True, kw_only=True)
class ArenaSensorDescription(SensorEntityDescription):
    value_fn: Callable[[AccountData], Any]
    attrs_fn: Callable[[AccountData], dict[str, Any]] | None = None


SENSORS: tuple[ArenaSensorDescription, ...] = (
    ArenaSensorDescription(
        key="loans",
        translation_key="loans",
        icon="mdi:book-open-variant",
        native_unit_of_measurement="lån",
        value_fn=lambda d: len(d.loans),
        attrs_fn=_loans_attrs,
    ),
    ArenaSensorDescription(
        key="next_due",
        translation_key="next_due",
        icon="mdi:calendar-clock",
        device_class=SensorDeviceClass.DATE,
        value_fn=_next_due,
        attrs_fn=_next_due_attrs,
    ),
    ArenaSensorDescription(
        key="reservations",
        translation_key="reservations",
        icon="mdi:book-clock",
        native_unit_of_measurement="st",
        value_fn=lambda d: len(d.reservations),
        attrs_fn=lambda d: {"reservations": [{**r.as_dict(), "cover": cover_url(r.record_id)} for r in d.reservations]},
    ),
    ArenaSensorDescription(
        key="reservations_ready",
        translation_key="reservations_ready",
        icon="mdi:book-check",
        native_unit_of_measurement="st",
        value_fn=lambda d: sum(1 for r in d.reservations if r.ready),
        attrs_fn=lambda d: {"titles": [r.title for r in d.reservations if r.ready]},
    ),
    ArenaSensorDescription(
        key="last_updated",
        translation_key="last_updated",
        icon="mdi:cloud-sync",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.fetched_at,
    ),
    ArenaSensorDescription(
        key="fees",
        translation_key="fees",
        icon="mdi:cash",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="SEK",
        value_fn=lambda d: d.fees,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ArenaConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(ArenaSensor(coordinator, entry, desc) for desc in SENSORS)


def device_info(entry: ArenaConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="Axiell",
        model="Arena",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=entry.data["base_url"] + "/protected/my-account/overview",
    )


class ArenaSensor(CoordinatorEntity[ArenaCoordinator], SensorEntity):
    _attr_has_entity_name = True
    entity_description: ArenaSensorDescription

    def __init__(self, coordinator: ArenaCoordinator, entry: ArenaConfigEntry, desc: ArenaSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = desc
        self._attr_unique_id = f"{entry.unique_id}_{desc.key}"
        self._attr_device_info = device_info(entry)

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attrs_fn
        attrs = fn(self.coordinator.data) if fn else {}
        if self.entity_description.key in ("loans", "last_updated"):
            # True while the library is unreachable and cached data is shown
            attrs["cached"] = self.coordinator.stale
        return attrs or None
