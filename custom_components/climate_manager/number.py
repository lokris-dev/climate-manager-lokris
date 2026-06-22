"""Number platform: per-zone thresholds + durations (editable from the UI)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_DUREE_COOLDOWN_MIN,
    CONF_DUREE_STABILISATION_MIN,
    CONF_OVERRIDE_DUREE_MIN,
    CONF_SEUIL_DEBUT_CHAUFFAGE,
    CONF_SEUIL_DEBUT_REFROIDISSEMENT,
    CONF_SEUIL_FIN_CHAUFFAGE,
    CONF_SEUIL_FIN_REFROIDISSEMENT,
    DOMAIN,
    MAX_DUREE_MIN,
    MAX_OVERRIDE_DUREE_MIN,
    MAX_SEUIL,
    MIN_DUREE_MIN,
    MIN_OVERRIDE_DUREE_MIN,
    MIN_SEUIL,
)
from .coordinator import DelormejClimateCoordinator
from .entity_base import DelormejClimateZoneEntity


@dataclass(frozen=True)
class _NumberSpec:
    suffix: str
    translation_key: str
    config_key: str
    minimum: float
    maximum: float
    step: float
    unit: str | None
    icon: str


_TEMP_SPECS: tuple[_NumberSpec, ...] = (
    _NumberSpec(
        "seuil_debut_chauffage",
        "seuil_debut_chauffage",
        CONF_SEUIL_DEBUT_CHAUFFAGE,
        MIN_SEUIL,
        MAX_SEUIL,
        0.5,
        UnitOfTemperature.CELSIUS,
        "mdi:thermometer-chevron-up",
    ),
    _NumberSpec(
        "seuil_fin_chauffage",
        "seuil_fin_chauffage",
        CONF_SEUIL_FIN_CHAUFFAGE,
        MIN_SEUIL,
        MAX_SEUIL,
        0.5,
        UnitOfTemperature.CELSIUS,
        "mdi:thermometer-check",
    ),
    _NumberSpec(
        "seuil_debut_refroidissement",
        "seuil_debut_refroidissement",
        CONF_SEUIL_DEBUT_REFROIDISSEMENT,
        MIN_SEUIL,
        MAX_SEUIL,
        0.5,
        UnitOfTemperature.CELSIUS,
        "mdi:thermometer-chevron-down",
    ),
    _NumberSpec(
        "seuil_fin_refroidissement",
        "seuil_fin_refroidissement",
        CONF_SEUIL_FIN_REFROIDISSEMENT,
        MIN_SEUIL,
        MAX_SEUIL,
        0.5,
        UnitOfTemperature.CELSIUS,
        "mdi:thermometer-check",
    ),
    _NumberSpec(
        "duree_stabilisation_min",
        "duree_stabilisation_min",
        CONF_DUREE_STABILISATION_MIN,
        MIN_DUREE_MIN,
        MAX_DUREE_MIN,
        1.0,
        "min",
        "mdi:timer-sand",
    ),
    _NumberSpec(
        "duree_cooldown_min",
        "duree_cooldown_min",
        CONF_DUREE_COOLDOWN_MIN,
        MIN_DUREE_MIN,
        MAX_DUREE_MIN,
        1.0,
        "min",
        "mdi:timer-cog",
    ),
    _NumberSpec(
        "override_duree_min",
        "override_duree_min",
        CONF_OVERRIDE_DUREE_MIN,
        MIN_OVERRIDE_DUREE_MIN,
        MAX_OVERRIDE_DUREE_MIN,
        1.0,
        "min",
        "mdi:account-clock-outline",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord: DelormejClimateCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[NumberEntity] = []
    for zid in coord.zones:
        for spec in _TEMP_SPECS:
            entities.append(ZoneNumber(coord, zid, spec))
    async_add_entities(entities)


class ZoneNumber(DelormejClimateZoneEntity, NumberEntity):
    _attr_mode = NumberMode.BOX
    # Seuils et durées = réglages admin → rangés dans la section Configuration de
    # l'appareil, hors du dashboard que voient les collègues.
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self, coord: DelormejClimateCoordinator, zone_id: str, spec: _NumberSpec
    ) -> None:
        super().__init__(coord, zone_id, spec.suffix)
        self._spec = spec
        self._attr_translation_key = spec.translation_key
        self._attr_native_min_value = spec.minimum
        self._attr_native_max_value = spec.maximum
        self._attr_native_step = spec.step
        self._attr_native_unit_of_measurement = spec.unit
        self._attr_icon = spec.icon

    @property
    def native_value(self) -> float | None:
        zone = self.coordinator.zone(self._zone_id)
        if not zone:
            return None
        return getattr(zone.config, self._spec.config_key, None)

    async def async_set_native_value(self, value: float) -> None:
        kwargs: dict[str, Any] = {self._spec.config_key: float(value)}
        # int for durations
        if self._spec.unit == "min":
            kwargs[self._spec.config_key] = int(value)
        self.coordinator.update_zone_config(self._zone_id, **kwargs)
        await self.coordinator.async_tick_now()
