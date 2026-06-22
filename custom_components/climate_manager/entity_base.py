"""Base entity for climate_manager platforms.

All entities are scoped to a single Zone — represented as an HA Device.
"""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import DelormejClimateCoordinator


class DelormejClimateZoneEntity(CoordinatorEntity[DelormejClimateCoordinator]):
    """Base class for any entity tied to a specific zone."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: DelormejClimateCoordinator, zone_id: str, unique_suffix: str
    ) -> None:
        super().__init__(coordinator)
        self._zone_id = zone_id
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{zone_id}_{unique_suffix}"

    @property
    def _zone_data(self) -> dict | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("zones", {}).get(self._zone_id)

    @property
    def available(self) -> bool:
        return super().available and self._zone_data is not None

    @property
    def device_info(self) -> DeviceInfo:
        zone = self.coordinator.zone(self._zone_id)
        name = zone.config.name if zone else self._zone_id
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.coordinator.entry.entry_id}_{self._zone_id}")},
            name=f"Climate Manager · {name}",
            manufacturer="delormej",
            model="Smart Climate Zone",
        )
