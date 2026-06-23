"""Sensor platform for climate_manager."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import DelormejClimateCoordinator
from .entity_base import DelormejClimateZoneEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord: DelormejClimateCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = []
    for zid in coord.zones:
        entities += [
            ZoneStateSensor(coord, zid),
            ZoneRegimeSensor(coord, zid),
            ZoneRoomTemperatureSensor(coord, zid),
            ZoneSetpointSentSensor(coord, zid),
            ZoneOverrideUntilSensor(coord, zid),
        ]
    async_add_entities(entities)


class ZoneStateSensor(DelormejClimateZoneEntity, SensorEntity):
    _attr_translation_key = "zone_state"
    _attr_icon = "mdi:state-machine"

    def __init__(self, coord: DelormejClimateCoordinator, zone_id: str) -> None:
        super().__init__(coord, zone_id, "state")

    @property
    def native_value(self) -> str | None:
        d = self._zone_data
        return d["state"] if d else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        d = self._zone_data
        if not d:
            return None
        cfg = d.get("config")
        attrs = {
            "zone_name": getattr(cfg, "name", None),
            "climate_entities": list(getattr(cfg, "climate_entities", []) or []),
            "temperature_sensors": list(getattr(cfg, "temperature_sensors", []) or []),
            "override_until_reset": getattr(cfg, "override_until_reset", False),
            "schedule_on": d.get("schedule_on"),
            "any_window_open": d.get("any_window_open"),
            "house_is_absent": d.get("house_is_absent"),
            "in_override": d.get("in_override"),
            "direction": d.get("direction"),
            "target_temperature": d.get("target_temperature"),
            "aggressivity": d.get("aggressivity"),
            "power": d.get("power"),
            "fan_intensity": d.get("fan_intensity"),
            "supports_cool": d.get("supports_cool", True),
            "supports_heat": d.get("supports_heat", True),
            "schedule_next_event": d.get("schedule_next_event"),
            "windows_open": d.get("windows_open"),
            "windows_total": d.get("windows_total"),
            "profiles": d.get("profiles", []),
            "active_profile_name": d.get("active_profile_name"),
            "cycle_history": d.get("cycle_history", []),
            "last_completed_cycle": (
                d.get("cycle_history")[-1] if d.get("cycle_history") else None
            ),
            "zone_id": self._zone_id,
        }
        for ts_key in (
            "state_entered_ts",
            "stabilization_ends_ts",
            "cooldown_ends_ts",
            "cycle_started_ts",
        ):
            ts = d.get(ts_key)
            if ts:
                attrs[ts_key.replace("_ts", "_at")] = datetime.fromtimestamp(ts, tz=UTC).isoformat()
        return attrs


class ZoneRegimeSensor(DelormejClimateZoneEntity, SensorEntity):
    _attr_translation_key = "zone_regime"
    _attr_icon = "mdi:gauge"

    def __init__(self, coord: DelormejClimateCoordinator, zone_id: str) -> None:
        super().__init__(coord, zone_id, "regime")

    @property
    def native_value(self) -> str | None:
        d = self._zone_data
        return d["regime"] if d else None


class ZoneRoomTemperatureSensor(DelormejClimateZoneEntity, SensorEntity):
    _attr_translation_key = "zone_room_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, coord: DelormejClimateCoordinator, zone_id: str) -> None:
        super().__init__(coord, zone_id, "room_temperature")

    @property
    def native_value(self) -> float | None:
        d = self._zone_data
        if not d or d.get("room_temperature") is None:
            return None
        return round(d["room_temperature"], 2)


class ZoneSetpointSentSensor(DelormejClimateZoneEntity, SensorEntity):
    _attr_translation_key = "zone_setpoint_sent"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, coord: DelormejClimateCoordinator, zone_id: str) -> None:
        super().__init__(coord, zone_id, "setpoint_sent")

    @property
    def native_value(self) -> float | None:
        d = self._zone_data
        return d.get("last_setpoint_sent") if d else None


class ZoneOverrideUntilSensor(DelormejClimateZoneEntity, SensorEntity):
    _attr_translation_key = "zone_override_until"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:account-clock"

    def __init__(self, coord: DelormejClimateCoordinator, zone_id: str) -> None:
        super().__init__(coord, zone_id, "override_until")

    @property
    def native_value(self) -> datetime | None:
        d = self._zone_data
        if not d:
            return None
        ts = d.get("override_until_ts")
        if ts is None:
            return None
        return datetime.fromtimestamp(ts, tz=UTC)
