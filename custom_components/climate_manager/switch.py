"""Switch platform: per-zone auto on/off."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, ZoneMode
from .coordinator import DelormejClimateCoordinator
from .entity_base import DelormejClimateZoneEntity
from .zone import utc_now_ts


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord: DelormejClimateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(ZoneAutoSwitch(coord, zid) for zid in coord.zones)


class ZoneAutoSwitch(DelormejClimateZoneEntity, SwitchEntity):
    _attr_translation_key = "zone_auto"
    _attr_icon = "mdi:auto-mode"

    def __init__(self, coord: DelormejClimateCoordinator, zone_id: str) -> None:
        super().__init__(coord, zone_id, "auto")

    @property
    def is_on(self) -> bool | None:
        d = self._zone_data
        if not d:
            return None
        return d.get("mode") != ZoneMode.OFF

    async def async_turn_on(self, **kwargs: Any) -> None:
        zone = self.coordinator.zone(self._zone_id)
        if not zone:
            return
        zone.set_mode(ZoneMode.AUTO, utc_now_ts())
        await self.coordinator.async_tick_now()

    async def async_turn_off(self, **kwargs: Any) -> None:
        zone = self.coordinator.zone(self._zone_id)
        if not zone:
            return
        zone.set_mode(ZoneMode.OFF, utc_now_ts())
        await self.coordinator.async_tick_now()
