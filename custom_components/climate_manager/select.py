"""Select platform: per-zone mode + aggressivity."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, FanIntensity, Power, SeasonMode, ZoneMode
from .coordinator import DelormejClimateCoordinator
from .entity_base import DelormejClimateZoneEntity
from .zone import utc_now_ts


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord: DelormejClimateCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SelectEntity] = [SeasonModeSelect(coord)]
    for zid in coord.zones:
        entities.append(ZoneModeSelect(coord, zid))
        entities.append(ZonePowerSelect(coord, zid))
        entities.append(ZoneFanIntensitySelect(coord, zid))
    async_add_entities(entities)


class SeasonModeSelect(CoordinatorEntity[DelormejClimateCoordinator], SelectEntity):
    """Sélecteur système du sens du groupe extérieur (mono-mode).

    Auto = le système choisit froid/chaud sur la T° moyenne bâtiment ;
    Été = force le froid ; Hiver = force le chaud. Impose UN seul sens à TOUTE
    la flotte (un seul groupe extérieur = un seul mode à la fois)."""

    _attr_has_entity_name = True
    _attr_translation_key = "season_mode"
    _attr_icon = "mdi:sun-snowflake-variant"
    _attr_options = SeasonMode.ALL

    def __init__(self, coord: DelormejClimateCoordinator) -> None:
        super().__init__(coord)
        self._attr_unique_id = f"{coord.entry.entry_id}_season_mode"

    @property
    def current_option(self) -> str | None:
        return self.coordinator.season_mode()

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_season_mode(option)


class ZoneModeSelect(DelormejClimateZoneEntity, SelectEntity):
    _attr_translation_key = "zone_mode"
    _attr_icon = "mdi:tune-variant"
    _attr_options = ZoneMode.ALL
    # Admin / services. Les collègues utilisent le switch Marche/Arrêt + Intensité.
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coord: DelormejClimateCoordinator, zone_id: str) -> None:
        super().__init__(coord, zone_id, "mode")

    @property
    def current_option(self) -> str | None:
        d = self._zone_data
        return d.get("mode") if d else None

    async def async_select_option(self, option: str) -> None:
        zone = self.coordinator.zone(self._zone_id)
        if not zone:
            return
        zone.set_mode(option, utc_now_ts())
        await self.coordinator.async_tick_now()


class ZonePowerSelect(DelormejClimateZoneEntity, SelectEntity):
    _attr_translation_key = "zone_power"
    _attr_icon = "mdi:flash"
    _attr_options = Power.ALL

    def __init__(self, coord: DelormejClimateCoordinator, zone_id: str) -> None:
        super().__init__(coord, zone_id, "power")

    @property
    def current_option(self) -> str | None:
        zone = self.coordinator.zone(self._zone_id)
        return zone.config.power if zone else None

    async def async_select_option(self, option: str) -> None:
        if option not in Power.ALL:
            return
        self.coordinator.update_zone_config(self._zone_id, power=option)
        await self.coordinator.async_tick_now()


class ZoneFanIntensitySelect(DelormejClimateZoneEntity, SelectEntity):
    _attr_translation_key = "zone_fan_intensity"
    _attr_icon = "mdi:fan"
    _attr_options = FanIntensity.ALL
    # Réglage admin (la ventilation suit l'intensité par défaut).
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coord: DelormejClimateCoordinator, zone_id: str) -> None:
        super().__init__(coord, zone_id, "fan_intensity")

    @property
    def current_option(self) -> str | None:
        zone = self.coordinator.zone(self._zone_id)
        return zone.config.fan_intensity if zone else None

    async def async_select_option(self, option: str) -> None:
        if option not in FanIntensity.ALL:
            return
        self.coordinator.update_zone_config(self._zone_id, fan_intensity=option)
        await self.coordinator.async_tick_now()
